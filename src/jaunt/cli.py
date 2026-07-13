"""CLI entry point for Jaunt.

Think about where you want to be, and you're there -- that's jaunting.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from jaunt import __version__
from jaunt.diagnostics import (
    format_build_failures,
    format_error_with_hint,
    format_test_generation_failures,
)
from jaunt.dotenv import load_dotenv_into_environ
from jaunt.errors import (
    JauntConfigError,
    JauntDependencyCycleError,
    JauntDiscoveryError,
    JauntGenerationError,
)
from jaunt.init_template import (
    INIT_SPEC_TEMPLATE,
    INIT_TEMPLATE,
    TYPESCRIPT_COMMONJS_TSCONFIG_TEMPLATE,
    TYPESCRIPT_CONTEXT_TEMPLATE,
    TYPESCRIPT_FACADE_TEMPLATE,
    TYPESCRIPT_INIT_TEMPLATE,
    TYPESCRIPT_SPEC_TEMPLATE,
    TYPESCRIPT_TSCONFIG_TEMPLATE,
    TYPESCRIPT_TEST_SPEC_TEMPLATE,
    TYPESCRIPT_TEST_TSCONFIG_TEMPLATE,
)
from jaunt.progress import ProgressBar
from jaunt.status_core import (
    compute_magic_status,
    deps_closure as _deps_closure,
    discover_targeted_test_entries as _discover_static_targeted_test_entries,
    iter_target_modules as _iter_target_modules,
    prepend_sys_path as _prepend_sys_path,
)

if TYPE_CHECKING:  # pragma: no cover
    from jaunt.config import JauntConfig
    from jaunt.cost import CostTracker
    from jaunt.generate.base import GeneratorBackend
    from jaunt.jobs import JobRecord
    from jaunt.registry import SpecEntry
    from jaunt.spec_ref import SpecRef
    from jaunt.workspace import ResolvedWorkspace


EXIT_OK = 0
EXIT_CONFIG_OR_DISCOVERY = 2
EXIT_GENERATION_ERROR = 3
EXIT_PYTEST_FAILURE = 4
EXIT_TIMEOUT = 5

_JOBS_WAIT_POLL_SECONDS = 1.0


def _add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root (defaults to searching upward from cwd for jaunt.toml).",
    )
    p.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to jaunt.toml (defaults to <root>/jaunt.toml).",
    )
    p.add_argument("--jobs", type=int, default=None, help="Concurrency override.")
    p.add_argument("--force", action="store_true", help="Force regeneration.")
    p.add_argument(
        "--target",
        action="append",
        default=[],
        help="Restrict to MODULE[:QUALNAME] (repeatable).",
    )
    p.add_argument(
        "--language",
        choices=("py", "ts"),
        default=None,
        help="Restrict a version-2 workspace command to Python or TypeScript.",
    )
    p.add_argument(
        "--no-infer-deps",
        action="store_true",
        help="Disable best-effort dependency inference (uses explicit deps only).",
    )
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable progress bars.",
    )
    p.add_argument(
        "--progress",
        choices=("auto", "rich", "plain", "none"),
        default="auto",
        help="Progress output mode (default: auto).",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass LLM response cache.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout (for agent/CI consumption).",
    )


def _add_build_generation_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--instruction",
        action="append",
        default=[],
        dest="instructions",
        help="Additional build instruction appended to the build prompt (repeatable).",
    )
    p.add_argument(
        "--include-target-tests",
        action="store_true",
        dest="include_target_tests",
        default=None,
        help="Include targeted test spec source in build prompts.",
    )
    p.add_argument(
        "--no-include-target-tests",
        action="store_false",
        dest="include_target_tests",
        help="Do not include targeted test spec source in build prompts.",
    )
    p.add_argument(
        "--no-auto-skills",
        action="store_true",
        dest="no_auto_skills",
        help="Disable auto-generated PyPI skill injection for this run.",
    )
    p.add_argument(
        "--no-builtin-skills",
        action="store_true",
        dest="no_builtin_skills",
        help="Do not seed Jaunt's bundled builtin skills into the Codex workspace.",
    )


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("must be a number") from e
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("must be a number") from e
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jaunt")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    build_p = subparsers.add_parser("build", help="Generate code for magic specs.")
    _add_common_flags(build_p)
    _add_build_generation_flags(build_p)
    build_p.add_argument(
        "--no-repo-map", action="store_true", help="Disable repo-map injection for this build."
    )
    build_p.add_argument(
        "--no-semantic-gate",
        action="store_true",
        dest="no_semantic_gate",
        help="Force every normalized-digest change to rebuild (skip the Layer B "
        "semantic gate). Layer A linter-resistance still applies.",
    )

    test_p = subparsers.add_parser("test", help="Generate tests and run the target test runner.")
    _add_common_flags(test_p)
    _add_build_generation_flags(test_p)
    test_p.add_argument("--no-build", action="store_true", help="Skip `jaunt build`.")
    test_p.add_argument("--no-run", action="store_true", help="Skip running pytest or Vitest.")
    test_p.add_argument(
        "--pytest-args",
        action="append",
        default=[],
        help="Extra args appended to pytest for Python targets (repeatable).",
    )
    test_p.add_argument(
        "--no-semantic-gate",
        action="store_true",
        dest="no_semantic_gate",
        help="Force every normalized-digest change to rebuild (skip the Layer B "
        "semantic gate). Layer A linter-resistance still applies.",
    )
    test_p.add_argument(
        "--no-redact-derived",
        action="store_true",
        dest="no_redact_derived",
        help="Feed FULL derived-tier failure detail (expected values, tracebacks) into "
        "repair. DANGER: defeats the held-out barrier; for debugging only.",
    )

    init_p = subparsers.add_parser("init", help="Initialize a new jaunt project.")
    init_p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Directory in which to create jaunt.toml (defaults to cwd).",
    )
    init_p.add_argument("--force", action="store_true", help="Overwrite existing jaunt.toml.")
    init_p.add_argument(
        "--language",
        choices=("py", "ts"),
        default="py",
        help="Scaffold a Python or TypeScript project (default: py).",
    )
    init_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    clean_p = subparsers.add_parser("clean", help="Remove __generated__ directories.")
    clean_p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root (defaults to searching upward for jaunt.toml).",
    )
    clean_p.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to jaunt.toml.",
    )
    clean_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting.",
    )
    clean_p.add_argument(
        "--orphans",
        action="store_true",
        help="Remove only orphaned generated artifacts (spec no longer exists).",
    )
    clean_p.add_argument(
        "--target",
        action="append",
        default=[],
        help="Restrict TypeScript cleanup to ts:<spec-path>[#symbol] (repeatable).",
    )
    clean_p.add_argument(
        "--language",
        choices=("py", "ts"),
        default=None,
        help="Restrict cleanup to one version-2 target language.",
    )
    clean_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    migrate_p = subparsers.add_parser(
        "migrate", help="Plan/apply mechanical source migrations (no model calls)."
    )
    migrate_p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root (defaults to searching upward for jaunt.toml).",
    )
    migrate_p.add_argument("--config", type=str, default=None, help="Path to jaunt.toml.")
    migrate_p.add_argument(
        "--language",
        choices=("py", "ts"),
        default=None,
        help="Restrict migration to one version-2 target language.",
    )
    migrate_p.add_argument(
        "--apply",
        action="store_true",
        help="Execute the migrations (default: plan only).",
    )
    migrate_p.add_argument(
        "--force",
        action="store_true",
        help="Apply even when the git working tree is dirty.",
    )
    migrate_p.add_argument(
        "--allow-newly-governed",
        action="store_true",
        dest="allow_newly_governed",
        help="Also rewrite ungoverned legacy stub bodies that would create new specs.",
    )
    migrate_p.add_argument(
        "--merge-projects",
        action="store_true",
        dest="merge_projects",
        help="Plan/apply consolidation of tracked descendant jaunt.toml files.",
    )
    migrate_p.add_argument(
        "--config-v2",
        action="store_true",
        dest="config_v2",
        help="Plan/apply the deterministic version-1 to version-2 config migration.",
    )
    migrate_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    status_p = subparsers.add_parser("status", help="Show project build status.")
    _add_common_flags(status_p)
    status_p.add_argument(
        "--magic-only",
        action="store_true",
        dest="magic_only",
        help="Probe only @jaunt.magic freshness; skip contract checks and repo-map drift.",
    )

    sync_p = subparsers.add_parser(
        "sync",
        help="Render TypeScript API mirrors and unbuilt placeholders without a model call.",
    )
    _add_common_flags(sync_p)

    design_p = subparsers.add_parser(
        "design",
        help="Propose or apply a TypeScript declaration for an @jauntDesign contract.",
    )
    _add_common_flags(design_p)
    design_p.set_defaults(language="ts")
    design_p.add_argument(
        "--apply",
        action="store_true",
        help="Apply the previously reviewed declaration patch without another model call.",
    )

    log_p = subparsers.add_parser("log", help="Show the JAUNT_LOG change journal.")
    log_p.add_argument("-n", "--lines", type=int, default=20, help="Number of lines (0 = all).")
    log_p.add_argument("--module", default=None, help="Filter by module name.")
    log_p.add_argument("--root", default=".", help="Project root.")
    log_p.add_argument("--json", action="store_true", dest="json_output")

    daemon_p = subparsers.add_parser("daemon", help="Background codegen daemon.")
    daemon_sub = daemon_p.add_subparsers(dest="daemon_command", required=True)
    daemon_start_p = daemon_sub.add_parser(
        "start",
        help="Run the daemon (foreground; Ctrl-C to stop).",
    )
    daemon_start_p.add_argument("--root", default=".")
    daemon_start_p.add_argument("--json", action="store_true", dest="json_output")
    daemon_stop_p = daemon_sub.add_parser("stop", help="Stop a running daemon.")
    daemon_stop_p.add_argument("--root", default=".")
    daemon_status_p = daemon_sub.add_parser("status", help="Show daemon and job status.")
    daemon_status_p.add_argument("--root", default=".")
    daemon_status_p.add_argument("--json", action="store_true", dest="json_output")

    jobs_p = subparsers.add_parser("jobs", help="Show daemon job records and pending staleness.")
    jobs_p.add_argument("--root", default=".")
    jobs_p.add_argument("--json", action="store_true", dest="json_output")
    jobs_sub = jobs_p.add_subparsers(dest="jobs_command")
    jobs_show_p = jobs_sub.add_parser("show", help="Show one job record.")
    jobs_show_p.add_argument("job_id")
    jobs_show_p.add_argument("--full", action="store_true", help="Include full local detail log.")
    jobs_show_p.add_argument("--root", default=argparse.SUPPRESS)
    jobs_show_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
    )
    jobs_retry_p = jobs_sub.add_parser("retry", help="Retry landing a parked job.")
    jobs_retry_p.add_argument("job_id")
    jobs_retry_p.add_argument("--root", default=argparse.SUPPRESS)
    jobs_retry_p.add_argument(
        "--force",
        action="store_true",
        help="Land even if the spec changed since the job parked.",
    )
    jobs_land_p = jobs_sub.add_parser("land", help="Land a parked proposal as a provenance commit.")
    jobs_land_p.add_argument("job_id", nargs="?")
    jobs_land_p.add_argument("--all", action="store_true")
    jobs_land_p.add_argument("--root", default=argparse.SUPPRESS)
    jobs_land_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
    )
    jobs_discard_p = jobs_sub.add_parser("discard", help="Discard a parked proposal.")
    jobs_discard_p.add_argument("job_id")
    jobs_discard_p.add_argument("--root", default=argparse.SUPPRESS)
    jobs_discard_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
    )
    jobs_wait_p = jobs_sub.add_parser("wait", help="Wait for daemon jobs to finish.")
    jobs_wait_p.add_argument("job_id", nargs="?")
    jobs_wait_p.add_argument("--root", default=argparse.SUPPRESS)
    jobs_wait_p.add_argument(
        "--timeout",
        type=_positive_float,
        default=None,
        help="Maximum seconds to wait before exiting 5.",
    )
    jobs_wait_p.add_argument(
        "--settle",
        type=_nonnegative_float,
        default=None,
        help="Idle seconds required before returning (default: 2 x daemon.poll_interval).",
    )
    jobs_wait_p.add_argument(
        "--progress",
        choices=("auto", "rich", "plain", "none"),
        default=argparse.SUPPRESS,
        help="Progress output mode (default: auto).",
    )
    jobs_wait_p.add_argument(
        "--no-progress",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable progress bars.",
    )
    jobs_wait_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
    )

    guard_p = subparsers.add_parser(
        "guard",
        help="PreToolUse hook: warn when agents touch generated code.",
    )
    guard_p.add_argument(
        "--generated-dir",
        default=None,
        help="Generated dir override (defaults to jaunt.toml or __generated__).",
    )

    plugin_p = subparsers.add_parser(
        "install-claude-plugin",
        help="Install the first-party Jaunt plugin into Claude Code.",
    )
    plugin_p.add_argument(
        "--local",
        action="store_true",
        help="Add the marketplace from this local clone instead of GitHub.",
    )
    plugin_p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root for --local (defaults to cwd).",
    )
    plugin_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    codex_plugin_p = subparsers.add_parser(
        "install-codex-plugin",
        help="Install the first-party Jaunt plugin into Codex.",
    )
    codex_plugin_p.add_argument(
        "--local",
        action="store_true",
        help="Add the marketplace from this local clone instead of GitHub.",
    )
    codex_plugin_p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root for --local (defaults to cwd).",
    )
    codex_plugin_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    instructions_p = subparsers.add_parser(
        "instructions",
        help="Print a project-aware agent primer for using Jaunt.",
    )
    instructions_p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root (defaults to searching upward for jaunt.toml).",
    )
    instructions_p.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to jaunt.toml (defaults to <root>/jaunt.toml).",
    )
    instructions_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    tree_p = subparsers.add_parser("tree", help="Maintain treedocs.yaml repo map.")
    _add_common_flags(tree_p)
    tree_p.add_argument("--check", action="store_true", help="Fail (exit 4) if the tree is stale.")
    tree_p.add_argument("--enrich", action="store_true", help="Force LLM enrichment this run.")
    tree_p.add_argument("--no-enrich", action="store_true", help="Force AST-only this run.")

    check_p = subparsers.add_parser(
        "check", help="Verify committed contract batteries (deterministic, no model)."
    )
    _add_common_flags(check_p)
    check_scope = check_p.add_mutually_exclusive_group()
    check_scope.add_argument(
        "--contracts-only",
        action="store_true",
        dest="contracts_only",
        help="Gate only contract batteries.",
    )
    check_scope.add_argument(
        "--magic-only",
        action="store_true",
        dest="magic_only",
        help="Gate only @jaunt.magic freshness.",
    )

    reconcile_p = subparsers.add_parser(
        "reconcile", help="Derive/refresh committed contract batteries (calls the model)."
    )
    _add_common_flags(reconcile_p)

    adopt_p = subparsers.add_parser("adopt", help="Adopt committed code and derive a battery.")
    adopt_p.add_argument("ref", help="Python 'module:func' or TypeScript 'path.ts#symbol' ref.")
    _add_common_flags(adopt_p)

    eject_p = subparsers.add_parser(
        "eject", help="Remove Jaunt tracking; leave ordinary code and tests."
    )
    eject_p.add_argument(
        "ref", nargs="?", default=None, help="Python 'module:func' or TypeScript target ref."
    )
    eject_p.add_argument("--all", action="store_true", help="Eject all contract functions.")
    _add_common_flags(eject_p)

    eval_p = subparsers.add_parser("eval", help="Run built-in eval suite against a real backend.")
    eval_p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root (defaults to searching upward for jaunt.toml).",
    )
    eval_p.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to jaunt.toml (defaults to <root>/jaunt.toml).",
    )
    eval_p.add_argument(
        "--provider",
        type=str,
        default=None,
        help="LLM provider override (defaults to [llm].provider).",
    )
    eval_p.add_argument(
        "--model",
        type=str,
        default=None,
        help="LLM model override (defaults to [llm].model).",
    )
    eval_p.add_argument(
        "--compare",
        action="append",
        nargs="+",
        default=[],
        help="Compare explicit targets in 'provider:model' format.",
    )
    eval_p.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run only selected eval case id(s) (repeatable).",
    )
    eval_p.add_argument(
        "--suite",
        type=str,
        default="codegen",
        choices=("codegen", "agent"),
        help="Eval suite to run.",
    )
    eval_p.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output directory root (defaults to <root>/.jaunt/evals).",
    )
    eval_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    watch_p = subparsers.add_parser("watch", help="Watch for changes and rebuild.")
    _add_common_flags(watch_p)
    _add_build_generation_flags(watch_p)
    watch_p.add_argument(
        "--test",
        action="store_true",
        dest="test",
        help="Run tests after each successful build.",
    )

    specs_p = subparsers.add_parser(
        "specs", help="List @jaunt.magic specs and their dependency graph."
    )
    specs_p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root (defaults to searching upward from cwd for jaunt.toml).",
    )
    specs_p.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to jaunt.toml (defaults to <root>/jaunt.toml).",
    )
    specs_p.add_argument(
        "--module",
        type=str,
        default=None,
        help="Restrict output to a single module.",
    )
    specs_p.add_argument(
        "--language",
        choices=("py", "ts"),
        default=None,
        help="Restrict listing to one version-2 target language.",
    )
    specs_p.add_argument(
        "--no-infer-deps",
        action="store_true",
        help="Disable best-effort dependency inference (uses explicit deps only).",
    )
    specs_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    cache_p = subparsers.add_parser("cache", help="Manage LLM response cache.")
    cache_sub = cache_p.add_subparsers(dest="cache_command", required=True)

    cache_info_p = cache_sub.add_parser("info", help="Show cache statistics.")
    cache_info_p.add_argument("--root", type=str, default=None)
    cache_info_p.add_argument("--config", type=str, default=None)
    cache_info_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    cache_clear_p = cache_sub.add_parser("clear", help="Clear all cached responses.")
    cache_clear_p.add_argument("--root", type=str, default=None)
    cache_clear_p.add_argument("--config", type=str, default=None)
    cache_clear_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit structured JSON output to stdout.",
    )

    # --- skill subcommand ---
    skill_p = subparsers.add_parser("skill", aliases=["skills"], help="Manage skills.")
    skill_sub = skill_p.add_subparsers(dest="skill_command", required=True)

    skill_list_p = skill_sub.add_parser("list", help="List all skills.")
    skill_list_p.add_argument("--root", type=str, default=None)
    skill_list_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output."
    )

    skill_add_p = skill_sub.add_parser("add", help="Add a new user skill.")
    skill_add_p.add_argument("name", help="Skill name.")
    skill_add_p.add_argument(
        "--description", "-d", type=str, default=None, help="Short description of the skill."
    )
    skill_add_p.add_argument(
        "--lib", "-l", action="append", default=[], dest="libs", help="PyPI package or local path."
    )
    skill_add_p.add_argument("--root", type=str, default=None)
    skill_add_p.add_argument("--json", action="store_true", dest="json_output", help="JSON output.")

    skill_remove_p = skill_sub.add_parser(
        "remove", aliases=["rm"], help="Remove a skill (requires -f)."
    )
    skill_remove_p.add_argument("name", help="Skill name.")
    skill_remove_p.add_argument("-f", "--force", action="store_true", help="Actually remove.")
    skill_remove_p.add_argument("--root", type=str, default=None)
    skill_remove_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output."
    )

    skill_show_p = skill_sub.add_parser("show", help="Show a skill's content.")
    skill_show_p.add_argument("name", help="Skill name.")
    skill_show_p.add_argument("--root", type=str, default=None)

    skill_refresh_p = skill_sub.add_parser("refresh", help="Refresh auto-generated skills.")
    skill_refresh_p.add_argument("--root", type=str, default=None)
    skill_refresh_p.add_argument("--config", type=str, default=None)
    skill_refresh_p.add_argument("--force", action="store_true", help="Remove and regenerate all.")
    skill_refresh_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output."
    )

    skill_import_p = skill_sub.add_parser("import", help="Import skills from ancestor dirs.")
    skill_import_p.add_argument("names", nargs="*", help="Exact skill names to import.")
    skill_import_p.add_argument("--root", type=str, default=None)
    skill_import_p.add_argument(
        "--from", type=str, default=None, dest="from_dir", help="Import from specific directory."
    )
    skill_import_p.add_argument(
        "--all",
        action="store_true",
        dest="import_all",
        help="Import all discoverable skills.",
    )
    skill_import_p.add_argument("--dry-run", action="store_true", help="Show what would import.")
    skill_import_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output."
    )

    skill_build_p = skill_sub.add_parser(
        "build", help="Elaborate a skill using LLM (requires --lib metadata)."
    )
    skill_build_p.add_argument("name", help="Skill name.")
    skill_build_p.add_argument("--root", type=str, default=None)
    skill_build_p.add_argument("--config", type=str, default=None)
    skill_build_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output."
    )

    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def _resolve_root_and_config(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    root = Path(args.root).resolve() if args.root else None
    config_path = Path(args.config).resolve() if args.config else None
    return root, config_path


def _load_config(args: argparse.Namespace) -> tuple[Path, JauntConfig]:
    from jaunt.config import find_project_root, load_config

    root, config_path = _resolve_root_and_config(args)
    if root is None and config_path is None:
        root = find_project_root(Path.cwd())
    elif root is None and config_path is not None:
        root = config_path.parent

    assert root is not None
    cfg = load_config(root=root, config_path=config_path)
    return root, cfg


def _target_dispatch_mode(args: argparse.Namespace, cfg: JauntConfig) -> str:
    """Return ``py``, ``ts``, or ``mixed`` without changing v1 defaults."""

    requested = getattr(args, "language", None)
    if cfg.version == 1:
        if requested == "ts":
            raise JauntConfigError("--language ts requires a version-2 jaunt.toml with [target.ts]")
        return "py"
    configured = cfg.target_languages
    if requested is not None:
        if requested not in configured:
            raise JauntConfigError(f"No [target.{requested}] is configured in jaunt.toml")
        return str(requested)
    if configured == ("ts",):
        return "ts"
    if configured == ("py",):
        return "py"
    if configured == ("py", "ts"):
        return "mixed"
    raise JauntConfigError("Version-2 jaunt.toml must configure [target.py] or [target.ts]")


def _typescript_target_ids(args: argparse.Namespace) -> tuple[str, ...]:
    values = tuple(str(value) for value in (getattr(args, "target", []) or []))
    invalid = [value for value in values if not value.startswith("ts:")]
    if invalid:
        raise JauntConfigError(
            "TypeScript targets use `ts:<root-relative-spec-path>[#symbol]`: " + ", ".join(invalid)
        )
    return values


def _emit_typescript_payload(payload: dict[str, object], *, json_mode: bool) -> None:
    if json_mode:
        _emit_json(payload)
        return
    from jaunt.typescript.cli_bridge import human_lines

    for line in human_lines(payload):
        print(line)


def _typescript_response_cache(args: argparse.Namespace, root: Path):
    from jaunt.cache import ResponseCache

    return ResponseCache(
        root / ".jaunt" / "cache",
        enabled=not bool(getattr(args, "no_cache", False)),
    )


def _typescript_builtin_skill_names(args: argparse.Namespace, cfg: JauntConfig) -> tuple[str, ...]:
    if not cfg.skills.builtin or bool(getattr(args, "no_builtin_skills", False)):
        return ()
    return tuple(cfg.skills.builtin_skills)


def _typescript_auto_skills_enabled(args: argparse.Namespace, cfg: JauntConfig) -> bool:
    return bool(cfg.skills.auto) and not bool(getattr(args, "no_auto_skills", False))


def _typescript_error(command: str, error: Exception, *, json_mode: bool, code: int) -> int:
    _print_error(error)
    if json_mode:
        diagnostic_code = getattr(error, "code", type(error).__name__)
        diagnostics = getattr(error, "diagnostics", ())
        _emit_json(
            {
                "schema_version": 2,
                "command": command,
                "ok": False,
                "error": {
                    "code": str(diagnostic_code),
                    "message": str(error),
                    "diagnostics": [
                        {
                            "code": getattr(item, "code", "JAUNT_TS_DIAGNOSTIC"),
                            "message": getattr(item, "message", str(item)),
                            "severity": getattr(item, "severity", "error"),
                            "path": getattr(item, "path", None),
                        }
                        for item in diagnostics
                    ],
                },
            }
        )
    return code


def _typescript_command_context(
    args: argparse.Namespace,
) -> tuple[Path, JauntConfig, str] | None:
    """Probe target dispatch while leaving v1 error rendering untouched."""

    try:
        root, cfg = _load_config(args)
    except (JauntConfigError, KeyError):
        return None
    try:
        mode = _target_dispatch_mode(args, cfg)
    except JauntConfigError as error:
        args._target_dispatch_error = error
        mode = "error"
    return root, cfg, mode


def _target_dispatch_failure(args: argparse.Namespace, mode: str) -> int | None:
    if mode != "error":
        return None
    error = getattr(args, "_target_dispatch_error", JauntConfigError("Invalid target selection"))
    return _typescript_error(
        str(getattr(args, "command", "command")),
        error,
        json_mode=_is_json_mode(args),
        code=EXIT_CONFIG_OR_DISCOVERY,
    )


def _cmd_typescript_build_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.builder import run_build
    from jaunt.typescript.cli_bridge import build_payload

    json_mode = _is_json_mode(args)
    try:
        report = asyncio.run(
            run_build(
                root,
                cfg,
                target_ids=_typescript_target_ids(args),
                force=bool(getattr(args, "force", False)),
                response_cache=_typescript_response_cache(args, root),
                jobs=getattr(args, "jobs", None),
                build_instructions=_effective_build_instructions(cfg, args),
                semantic_gate_enabled=(
                    False if bool(getattr(args, "no_semantic_gate", False)) else None
                ),
                repo_map_enabled=bool(cfg.context.repo_map)
                and not bool(getattr(args, "no_repo_map", False)),
                auto_skills_enabled=_typescript_auto_skills_enabled(args, cfg),
                builtin_skill_names=_typescript_builtin_skill_names(args, cfg),
            )
        )
        payload = build_payload(report)
        _emit_typescript_payload(payload, json_mode=json_mode)
        return report.exit_code
    except JauntGenerationError as error:
        return _typescript_error("build", error, json_mode=json_mode, code=EXIT_GENERATION_ERROR)
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error("build", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY)


def _cmd_typescript_test_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import test_payload
    from jaunt.typescript.tester import run_test

    json_mode = _is_json_mode(args)
    try:
        report = asyncio.run(
            run_test(
                root,
                cfg,
                target_ids=_typescript_target_ids(args),
                no_build=bool(getattr(args, "no_build", False)),
                no_run=bool(getattr(args, "no_run", False)),
                no_redact_derived=bool(getattr(args, "no_redact_derived", False)),
                force=bool(getattr(args, "force", False)),
                response_cache=_typescript_response_cache(args, root),
                jobs=getattr(args, "jobs", None),
                build_instructions=_effective_build_instructions(cfg, args),
                semantic_gate_enabled=(
                    False if bool(getattr(args, "no_semantic_gate", False)) else None
                ),
                repo_map_enabled=bool(cfg.context.repo_map)
                and not bool(getattr(args, "no_repo_map", False)),
                auto_skills_enabled=_typescript_auto_skills_enabled(args, cfg),
                builtin_skill_names=_typescript_builtin_skill_names(args, cfg),
            )
        )
        payload = test_payload(report)
        _emit_typescript_payload(payload, json_mode=json_mode)
        return report.exit_code
    except JauntGenerationError as error:
        return _typescript_error("test", error, json_mode=json_mode, code=EXIT_GENERATION_ERROR)
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error("test", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY)


def cmd_sync(args: argparse.Namespace) -> int:
    from jaunt.typescript.builder import run_sync
    from jaunt.typescript.cli_bridge import sync_payload

    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        mode = _target_dispatch_mode(args, cfg)
        if mode == "py":
            raise JauntConfigError("`jaunt sync` currently operates on [target.ts]")
        report = asyncio.run(run_sync(root, cfg, target_ids=_typescript_target_ids(args)))
        payload = sync_payload(report)
        _emit_typescript_payload(payload, json_mode=json_mode)
        return report.exit_code
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error("sync", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY)


def cmd_design(args: argparse.Namespace) -> int:
    from jaunt.typescript.cli_bridge import design_payload
    from jaunt.typescript.design import run_design

    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        mode = _target_dispatch_mode(args, cfg)
        if mode == "py":
            raise JauntConfigError("`jaunt design` is TypeScript-only")
        target_ids = _typescript_target_ids(args)
        if len(target_ids) > 1:
            raise JauntConfigError("`jaunt design` accepts at most one --target")
        report = asyncio.run(
            run_design(
                root,
                cfg,
                target_id=target_ids[0] if target_ids else None,
                apply=bool(getattr(args, "apply", False)),
                force=bool(getattr(args, "force", False)),
            )
        )
        payload = design_payload(report)
        _emit_typescript_payload(payload, json_mode=json_mode)
        return report.exit_code
    except JauntGenerationError as error:
        return _typescript_error("design", error, json_mode=json_mode, code=EXIT_GENERATION_ERROR)
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error(
            "design", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY
        )


def _cmd_typescript_adopt_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import lifecycle_payload
    from jaunt.typescript.contracts import run_adopt

    json_mode = _is_json_mode(args)
    try:
        report = asyncio.run(
            run_adopt(
                root,
                cfg,
                target=str(args.ref),
                apply=True,
                response_cache=_typescript_response_cache(args, root),
                auto_skills_enabled=_typescript_auto_skills_enabled(args, cfg),
                builtin_skill_names=_typescript_builtin_skill_names(args, cfg),
            )
        )
        _emit_typescript_payload(lifecycle_payload(report), json_mode=json_mode)
        return report.exit_code
    except JauntGenerationError as error:
        return _typescript_error("adopt", error, json_mode=json_mode, code=EXIT_GENERATION_ERROR)
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error("adopt", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY)


def _cmd_typescript_reconcile_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import lifecycle_payload
    from jaunt.typescript.contracts import run_reconcile

    json_mode = _is_json_mode(args)
    try:
        report = asyncio.run(
            run_reconcile(
                root,
                cfg,
                target_ids=_typescript_target_ids(args),
                response_cache=_typescript_response_cache(args, root),
                auto_skills_enabled=_typescript_auto_skills_enabled(args, cfg),
                builtin_skill_names=_typescript_builtin_skill_names(args, cfg),
            )
        )
        _emit_typescript_payload(lifecycle_payload(report), json_mode=json_mode)
        return report.exit_code
    except JauntGenerationError as error:
        return _typescript_error(
            "reconcile", error, json_mode=json_mode, code=EXIT_GENERATION_ERROR
        )
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error(
            "reconcile", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY
        )


def _cmd_typescript_eject_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import lifecycle_payload
    from jaunt.typescript.contracts import run_eject

    json_mode = _is_json_mode(args)
    try:
        if bool(getattr(args, "all", False)) or not getattr(args, "ref", None):
            raise JauntConfigError(
                "TypeScript ejection requires one path#symbol or ts:module target"
            )
        report = asyncio.run(run_eject(root, cfg, target=str(args.ref)))
        _emit_typescript_payload(lifecycle_payload(report), json_mode=json_mode)
        return report.exit_code
    except JauntGenerationError as error:
        return _typescript_error("eject", error, json_mode=json_mode, code=EXIT_GENERATION_ERROR)
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error("eject", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY)


def _cmd_typescript_status_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import status_payload
    from jaunt.typescript.status import run_status

    json_mode = _is_json_mode(args)
    try:
        report = asyncio.run(run_status(root, cfg, target_ids=_typescript_target_ids(args)))
        payload = status_payload(report)
        _emit_typescript_payload(payload, json_mode=json_mode)
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error(
            "status", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY
        )


def _cmd_typescript_check_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import check_payload
    from jaunt.typescript.status import run_check

    json_mode = _is_json_mode(args)
    try:
        report = asyncio.run(
            run_check(
                root,
                cfg,
                target_ids=_typescript_target_ids(args),
                magic_only=bool(getattr(args, "magic_only", False)),
                contracts_only=bool(getattr(args, "contracts_only", False)),
            )
        )
        payload = check_payload(report)
        _emit_typescript_payload(payload, json_mode=json_mode)
        return report.exit_code
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error("check", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY)


def _cmd_typescript_specs_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import specs_payload
    from jaunt.typescript.status import run_specs

    json_mode = _is_json_mode(args)
    try:
        target_ids = _typescript_target_ids(args)
        module_filter = getattr(args, "module", None)
        if module_filter:
            target_ids = (*target_ids, str(module_filter))
        report = asyncio.run(run_specs(root, cfg, target_ids=target_ids))
        payload = specs_payload(report)
        _emit_typescript_payload(payload, json_mode=json_mode)
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error("specs", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY)


def _cmd_typescript_clean_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import clean_payload
    from jaunt.typescript.status import run_clean

    json_mode = _is_json_mode(args)
    try:
        report = asyncio.run(
            run_clean(
                root,
                cfg,
                target_ids=_typescript_target_ids(args),
                orphans_only=bool(getattr(args, "orphans", False)),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
        )
        payload = clean_payload(report)
        _emit_typescript_payload(payload, json_mode=json_mode)
        return report.exit_code
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error("clean", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY)


def _capture_python_json(
    command: Callable[[argparse.Namespace], int], args: argparse.Namespace
) -> tuple[int, dict[str, object]]:
    """Run the unchanged Python renderer in JSON mode for v2 aggregation."""

    import contextlib
    import io

    child = argparse.Namespace(**vars(args))
    child.language = "py"

    def empty_payload() -> dict[str, object]:
        return {
            "command": str(getattr(args, "command", "")),
            "ok": True,
            "generated": [],
            "skipped": [],
            "refrozen": [],
            "failed": {},
            "fresh": [],
            "stale": [],
            "stale_changes": {},
            "orphans": [],
            "checked": [],
            "blocked": [],
            "specs": [],
            "dependency_graph": {},
            "magic": {},
        }

    if hasattr(child, "target"):
        original_targets = list(getattr(child, "target", []) or [])
        child.target = [
            str(value).removeprefix("py:")
            for value in original_targets
            if not str(value).startswith("ts:")
        ]
        if original_targets and not child.target:
            return 0, empty_payload()
    module_filter = getattr(child, "module", None)
    if isinstance(module_filter, str):
        if module_filter.startswith("ts:"):
            return 0, empty_payload()
        child.module = module_filter.removeprefix("py:")
    child.json_output = True
    child.no_progress = True
    child.progress = "none"
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = command(child)
    for line in reversed(output.getvalue().splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return exit_code, cast("dict[str, object]", value)
    return exit_code, {"ok": exit_code == 0}


def _qualify_python_ids(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item) if str(item).startswith("py:") else f"py:{item}" for item in values]


def _payload_list(payload: dict[str, object], key: str) -> list[object]:
    value = payload.get(key, [])
    return list(value) if isinstance(value, list) else []


def _aggregate_cost_payloads(*values: object) -> dict[str, object]:
    costs = [cast("dict[str, object]", value) for value in values if isinstance(value, dict)]
    if not costs:
        return {}
    integer_fields = (
        "api_calls",
        "cache_hits",
        "prompt_tokens",
        "cached_prompt_tokens",
        "completion_tokens",
        "total_tokens",
    )
    combined: dict[str, object] = {}
    for field in integer_fields:
        total = 0
        for cost in costs:
            raw = cost.get(field)
            if isinstance(raw, int):
                total += raw
        combined[field] = total
    estimated_cost = 0.0
    for cost in costs:
        raw = cost.get("estimated_cost_usd")
        if isinstance(raw, (int, float)):
            estimated_cost += float(raw)
    combined["estimated_cost_usd"] = round(estimated_cost, 6)
    return combined


def _mixed_typescript_targets(args: argparse.Namespace) -> tuple[str, ...] | None:
    """Return selected TS IDs, or ``None`` when explicit targets select only Python."""

    values = tuple(str(value) for value in (getattr(args, "target", []) or []))
    selected = tuple(value for value in values if value.startswith("ts:"))
    return None if values and not selected else selected


def _mixed_runtime_args(
    args: argparse.Namespace,
    cfg: JauntConfig,
    *,
    command: Literal["build", "test", "reconcile"],
) -> argparse.Namespace:
    """Clone CLI args with one shared model-call runtime for both targets."""

    from jaunt.targets.runtime import MixedTargetRuntime

    configured_jobs = cfg.test.jobs if command == "test" else cfg.build.jobs
    jobs = int(args.jobs) if getattr(args, "jobs", None) is not None else int(configured_jobs)
    if jobs < 1:
        raise JauntConfigError("Mixed-target jobs must be >= 1")
    child = argparse.Namespace(**vars(args))
    child._mixed_runtime = MixedTargetRuntime(
        jobs=jobs,
        max_cost=cfg.llm.max_cost_per_build,
    )
    return child


async def _await_mixed_with_signals(operation, runtime):
    """Turn SIGTERM into task cancellation so process-tree cleanup can run."""

    import signal

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = False
    previous = None
    if task is not None:
        try:
            previous = signal.getsignal(signal.SIGTERM)
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            installed = False
    try:
        return await operation
    except asyncio.CancelledError:
        runtime.cancel()
        raise
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)
            assert previous is not None
            signal.signal(signal.SIGTERM, previous)


def _prepare_mixed_repo_map(
    args: argparse.Namespace,
    root: Path,
    cfg: JauntConfig,
) -> None:
    """Render one deterministic repo map for both concurrent language builds."""

    enabled = bool(cfg.context.repo_map) and not bool(getattr(args, "no_repo_map", False))
    args._mixed_repo_map_enabled = enabled
    if not enabled:
        args._mixed_repo_map_block = None
        return
    if cfg.context.enrich:
        _eprint(
            "warn: deferred model-enriched repo-map descriptions for this mixed-target "
            "command; run `jaunt tree --enrich` separately"
        )
    try:
        from jaunt.repo_context import api as rc_api
        from jaunt.repo_context import block as rc_block

        repo_map_doc, _ = rc_api.sync_tree(
            root=root,
            cfg=cfg,
            today=_today(),
            # The legacy enrich backend is not command-runtime aware.  One
            # shared AST map keeps both targets deterministic and model-free.
            enrich=False,
        )
        args._mixed_repo_map_block = rc_block.render_repo_map(
            repo_map_doc,
            max_chars=cfg.context.max_chars,
        )
    except Exception:  # noqa: BLE001 - repo map remains best-effort
        args._mixed_repo_map_block = ""


def _prepare_mixed_typescript_skills(
    args: argparse.Namespace,
    root: Path,
    cfg: JauntConfig,
) -> None:
    """Seed deterministic npm skills before either language fingerprints them."""

    builtin_enabled = bool(cfg.skills.builtin) and not bool(
        getattr(args, "no_builtin_skills", False)
    )
    args._mixed_builtin_skill_names = tuple(cfg.skills.builtin_skills) if builtin_enabled else ()
    auto_enabled = bool(cfg.skills.auto) and not bool(getattr(args, "no_auto_skills", False))
    args._mixed_npm_skill_metadata = {}
    if not auto_enabled:
        return
    from jaunt.skills_npm import ensure_npm_skills, typescript_package_owners

    target = cfg.typescript_target
    if target is None:
        return
    result = ensure_npm_skills(
        project_root=root,
        package_owners=typescript_package_owners(root, target),
        max_readme_chars=cfg.skills.max_chars_per_skill,
    )
    args._mixed_npm_skill_metadata = {
        "generated": result.generated,
        "skipped": result.skipped,
        "removed": result.removed,
        "warnings": result.warnings,
    }
    for warning in result.warnings:
        _eprint(f"warn: {warning}")


def _merge_exit_codes(*codes: int) -> int:
    from jaunt.targets.orchestrator import aggregate_exit_code

    return aggregate_exit_code(codes)


def _mixed_operation_error(
    command: str,
    error: Exception,
    args: argparse.Namespace,
    *,
    python_code: int,
    python_payload: dict[str, object],
) -> int:
    """Render one structured failure when the TS half of a mixed command aborts."""

    typescript_code = (
        EXIT_GENERATION_ERROR
        if isinstance(error, JauntGenerationError)
        else EXIT_CONFIG_OR_DISCOVERY
    )
    exit_code = _merge_exit_codes(python_code, typescript_code)
    diagnostic = {
        "code": str(getattr(error, "code", type(error).__name__)),
        "message": str(error),
    }
    python_target: dict[str, object] = dict(python_payload)
    typescript_target: dict[str, object] = {"ok": False, "error": diagnostic}
    runtime = getattr(args, "_mixed_runtime", None)
    aggregate_cost: dict[str, object] = {}
    if runtime is not None:
        python_cost = runtime.summary("py")
        typescript_cost = runtime.summary("ts")
        python_target["cost"] = python_cost
        typescript_target["cost"] = typescript_cost
        aggregate_cost = _aggregate_cost_payloads(python_cost, typescript_cost)
    payload: dict[str, object] = {
        "schema_version": 2,
        "command": command,
        "ok": False,
        "error": diagnostic,
        "targets": {
            "py": python_target,
            "ts": typescript_target,
        },
    }
    if aggregate_cost:
        payload["cost"] = aggregate_cost
    _print_error(error)
    if _is_json_mode(args):
        _emit_json(payload)
    else:
        print(f"Python {command}: {'ok' if python_code == 0 else 'failed'}")
        print(f"TypeScript {command}: failed")
    return exit_code


def _mixed_typescript_preflight(
    root: Path,
    cfg: JauntConfig,
    target_ids: tuple[str, ...],
    *,
    reject_pending_designs: bool = True,
    for_test: bool = False,
) -> object:
    """Finish TS config/discovery before a mixed command may mutate Python outputs."""

    from jaunt.typescript.builder import analyze, worker_session

    async def inspect_workspace():
        async with worker_session(root, cfg) as (client, initialized):
            target_analysis = await analyze(client, initialized, target_ids=target_ids)
            pending_designs = [
                str(module.get("moduleId", module.get("id", "")))
                for module in target_analysis.modules
                if "@jauntDesign" in str(module.get("specSource", ""))
            ]
            if reject_pending_designs and pending_designs:
                raise JauntConfigError(
                    "TypeScript declarations still require reviewable design; run "
                    "`jaunt design --target <module#symbol>` first: "
                    + ", ".join(sorted(pending_designs))
                )
            analysis = (
                await analyze(client, initialized) if for_test and target_ids else target_analysis
            )
            if for_test:
                from jaunt.typescript.tester import (
                    _group_test_files,
                    _module_id,
                    _owner_project_for_source,
                    _runner_path,
                    _selected_generated_test_files,
                    _selected_test_modules,
                    _selected_test_specs,
                    _test_output,
                    _validate_test_owner_dependencies,
                    _workspace_test_file_owners,
                )

                _runner_path(client)
                target = cfg.typescript_target
                assert target is not None
                modules = {_module_id(module): module for module in analysis.modules}
                test_specs = _selected_test_specs(
                    root,
                    cfg,
                    analysis.workspace,
                    modules,
                    target_ids=target_ids,
                )
                files = set(
                    _selected_generated_test_files(
                        root,
                        cfg,
                        test_specs,
                        target_ids=target_ids,
                    )
                )
                owners = dict(_workspace_test_file_owners(root, cfg, analysis.workspace))
                require_fast_check = False
                for spec in test_specs:
                    path = str(spec.get("path", ""))
                    owner = spec.get("project")
                    if not isinstance(owner, str):
                        owner = _owner_project_for_source(
                            root,
                            cfg,
                            analysis.workspace,
                            path,
                        )
                    for tier in ("example", "derived"):
                        output = _test_output(path, target.generated_dir, tier)
                        files.add(output)
                        owners[output] = owner
                    source = spec.get("syntheticSource")
                    if not isinstance(source, str):
                        source = (root / path).read_text(encoding="utf-8")
                    contract_sources = (
                        str(module.get("specSource", ""))
                        for module in _selected_test_modules(spec, modules)
                    )
                    require_fast_check = require_fast_check or any(
                        "@prop" in candidate for candidate in (source, *contract_sources)
                    )
                grouped = _group_test_files(
                    root,
                    cfg,
                    analysis.workspace,
                    tuple(sorted(files)),
                    explicit_owners=owners,
                )
                if grouped:
                    _validate_test_owner_dependencies(
                        root,
                        analysis.workspace,
                        grouped,
                        require_fast_check=require_fast_check,
                    )
            return analysis

    return asyncio.run(inspect_workspace())


def _mixed_python_preflight(command: str, args: argparse.Namespace) -> None:
    """Run Python discovery/cycle checks before concurrent target mutation."""

    raw_targets = tuple(str(item) for item in (getattr(args, "target", []) or []))
    if raw_targets and all(item.startswith("ts:") for item in raw_targets):
        return
    if command == "test" and not bool(getattr(args, "no_run", False)):
        from jaunt import tester

        tester.ensure_pytest_available()
    code, payload = _capture_python_json(cmd_status, args)
    if code == EXIT_OK:
        return
    raw_error = payload.get("error", "Python target discovery failed")
    if isinstance(raw_error, dict):
        error_record = cast("dict[str, object]", raw_error)
        message = str(error_record.get("message", "Python target discovery failed"))
    else:
        message = str(raw_error)
    raise JauntDiscoveryError(message)


def _validated_typescript_contract_targets(
    analysis: object,
    requested: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate/expand selected TS contract IDs from the preflight snapshot."""

    if not requested:
        return ()
    workspace = getattr(analysis, "workspace", None)
    if not isinstance(workspace, Mapping):
        raise JauntDiscoveryError("TypeScript contract preflight returned no workspace snapshot")
    records = workspace.get("contracts", [])
    exact: set[str] = set()
    by_module: dict[str, set[str]] = {}
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, Mapping) or not isinstance(record.get("path"), str):
                continue
            path = str(record["path"])
            module_id = f"ts:{Path(path).with_suffix('').as_posix()}"
            symbols = record.get("symbols", [])
            if not isinstance(symbols, list):
                continue
            for raw_symbol in symbols:
                symbol = (
                    str(raw_symbol.get("name"))
                    if isinstance(raw_symbol, Mapping)
                    else str(raw_symbol)
                )
                target = f"{module_id}#{symbol}"
                exact.add(target)
                by_module.setdefault(module_id, set()).add(target)

    expanded: list[str] = []
    unmatched: list[str] = []
    for target in requested:
        if target in exact:
            expanded.append(target)
        elif target in by_module:
            expanded.extend(sorted(by_module[target]))
        else:
            unmatched.append(target)
    if unmatched:
        raise JauntConfigError(
            "No TypeScript contract matches target(s): " + ", ".join(sorted(unmatched))
        )
    return tuple(dict.fromkeys(expanded))


def _mixed_preflight_error(
    command: str,
    error: Exception,
    args: argparse.Namespace,
    *,
    language: Literal["py", "ts"] = "ts",
) -> int:
    diagnostic = {
        "code": str(getattr(error, "code", type(error).__name__)),
        "message": str(error),
    }
    other_language = "py" if language == "ts" else "ts"
    skipped = {
        "ok": False,
        "skipped": True,
        "reason": f"{language.upper()} preflight failed before {other_language} execution",
    }
    payload: dict[str, object] = {
        "schema_version": 2,
        "command": command,
        "ok": False,
        "error": diagnostic,
        "generated": [],
        "skipped": [],
        "refrozen": [],
        "failed": {f"{language}:workspace": [diagnostic]},
        "targets": {
            language: {"ok": False, "error": diagnostic},
            other_language: skipped,
        },
    }
    _print_error(error)
    if _is_json_mode(args):
        _emit_json(payload)
    else:
        failed_label = "Python" if language == "py" else "TypeScript"
        skipped_label = "TypeScript" if language == "py" else "Python"
        print(f"{skipped_label} {command}: not run")
        print(f"{failed_label} {command}: preflight failed")
    return EXIT_CONFIG_OR_DISCOVERY


def _emit_mixed_payload(
    payload: dict[str, object],
    *,
    json_mode: bool,
    python_payload: dict[str, object],
    typescript_payload: dict[str, object],
) -> None:
    if json_mode:
        _emit_json(payload)
        return
    print(f"Python {payload['command']}:")
    for key in ("generated", "skipped", "refrozen", "fresh", "stale", "unbuilt"):
        values = python_payload.get(key)
        if isinstance(values, list):
            print(f"  {key}: {len(values)}")
            for value in values:
                print(f"    - {value}")
    from jaunt.typescript.cli_bridge import human_lines

    for line in human_lines(typescript_payload):
        print(line)


def _mixed_build_payload(
    command: str,
    python_payload: dict[str, object],
    typescript_payload: dict[str, object],
    *,
    exit_code: int,
) -> dict[str, object]:
    py_generated = _qualify_python_ids(python_payload.get("generated"))
    py_skipped = _qualify_python_ids(python_payload.get("skipped"))
    py_refrozen = _qualify_python_ids(python_payload.get("refrozen"))
    ts_generated = list(cast("list[str]", typescript_payload.get("generated", [])))
    ts_skipped = list(cast("list[str]", typescript_payload.get("skipped", [])))
    ts_refrozen = list(cast("list[str]", typescript_payload.get("refrozen", [])))
    failed: dict[str, object] = {}
    py_failed = python_payload.get("failed", {})
    if command == "test" and isinstance(python_payload.get("generation_failed"), dict):
        py_failed = python_payload["generation_failed"]
    if isinstance(py_failed, dict):
        failed.update({f"py:{key}": value for key, value in py_failed.items()})
    ts_failed = typescript_payload.get("failed", {})
    if isinstance(ts_failed, dict):
        failed.update({str(key): value for key, value in ts_failed.items()})
    py_target = {
        "generated": _payload_list(python_payload, "generated"),
        "skipped": _payload_list(python_payload, "skipped"),
        "refrozen": _payload_list(python_payload, "refrozen"),
        "failed": py_failed,
    }
    py_cost = python_payload.get("cost")
    ts_cost = typescript_payload.get("cost")
    if isinstance(py_cost, dict):
        py_target["cost"] = py_cost
    if command == "test" and "pytest" in python_payload:
        py_target["pytest"] = python_payload["pytest"]
    if command == "test" and "generation_failed" in python_payload:
        py_target["generation_failed"] = python_payload["generation_failed"]
    if command == "test" and "owners" in python_payload:
        py_target["owners"] = python_payload["owners"]
    ts_targets = typescript_payload.get("targets", {})
    ts_target = (
        cast("dict[str, object]", ts_targets).get("ts", {}) if isinstance(ts_targets, dict) else {}
    )
    if isinstance(ts_target, dict) and isinstance(ts_cost, dict):
        ts_target = {**ts_target, "cost": ts_cost}
    payload: dict[str, object] = {
        "schema_version": 2,
        "command": command,
        "ok": exit_code == 0,
        "generated": sorted([*py_generated, *ts_generated]),
        "skipped": sorted([*py_skipped, *ts_skipped]),
        "refrozen": sorted([*py_refrozen, *ts_refrozen]),
        "failed": failed,
        "targets": {"py": py_target, "ts": ts_target},
    }
    if command == "test":
        if "pytest" in python_payload:
            payload["pytest"] = python_payload["pytest"]
        if "vitest" in typescript_payload:
            payload["vitest"] = typescript_payload["vitest"]
        if "owners" in python_payload:
            payload["owners"] = python_payload["owners"]
    aggregate_cost = _aggregate_cost_payloads(py_cost, ts_cost)
    if aggregate_cost:
        payload["cost"] = aggregate_cost
    return payload


def _cmd_mixed_build(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.builder import run_build
    from jaunt.typescript.cli_bridge import build_payload

    target_ids = _mixed_typescript_targets(args)
    if target_ids is not None:
        try:
            _mixed_typescript_preflight(root, cfg, target_ids)
        except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
            return _mixed_preflight_error("build", error, args)
    try:
        _mixed_python_preflight("build", args)
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _mixed_preflight_error("build", error, args, language="py")
    if target_ids is None:
        from jaunt.targets.base import TargetBuildReport

        py_code, py_payload = _capture_python_json(cmd_build, args)
        report = TargetBuildReport(language="ts")
        ts_payload = build_payload(report)
        exit_code = _merge_exit_codes(py_code)
        payload = _mixed_build_payload("build", py_payload, ts_payload, exit_code=exit_code)
        _emit_mixed_payload(
            payload,
            json_mode=_is_json_mode(args),
            python_payload=py_payload,
            typescript_payload=ts_payload,
        )
        return exit_code

    try:
        mixed_args = _mixed_runtime_args(args, cfg, command="build")
    except JauntConfigError as error:
        return _mixed_preflight_error("build", error, args)
    _prepare_mixed_typescript_skills(mixed_args, root, cfg)
    _prepare_mixed_repo_map(mixed_args, root, cfg)

    async def run_both() -> tuple[object, object]:
        try:
            return cast(
                "tuple[object, object]",
                await asyncio.gather(
                    asyncio.to_thread(_capture_python_json, cmd_build, mixed_args),
                    mixed_args._mixed_runtime.run_operation(
                        run_build(
                            root,
                            cfg,
                            target_ids=target_ids,
                            force=bool(getattr(args, "force", False)),
                            generator=_command_backend(mixed_args, cfg, "ts"),
                            cost_tracker=_command_cost_tracker(mixed_args, cfg, "ts"),
                            response_cache=_typescript_response_cache(mixed_args, root),
                            jobs=getattr(args, "jobs", None),
                            build_instructions=_effective_build_instructions(cfg, args),
                            semantic_gate_enabled=(
                                False if bool(getattr(args, "no_semantic_gate", False)) else None
                            ),
                            semantic_gate_exec=_command_semantic_exec(
                                mixed_args,
                                language="ts",
                                charge_usage=False,
                            ),
                            repo_map_enabled=bool(mixed_args._mixed_repo_map_enabled),
                            repo_map_block_override=mixed_args._mixed_repo_map_block,
                            auto_skills_enabled=False,
                            builtin_skill_names=mixed_args._mixed_builtin_skill_names,
                        ),
                    ),
                    return_exceptions=True,
                ),
            )
        except BaseException:
            mixed_args._mixed_runtime.cancel()
            raise

    python_result, typescript_result = asyncio.run(
        _await_mixed_with_signals(run_both(), mixed_args._mixed_runtime)
    )
    if isinstance(python_result, asyncio.CancelledError):
        ts_failed = (
            isinstance(typescript_result, BaseException)
            or int(getattr(typescript_result, "exit_code", 0)) != 0
        )
        if not ts_failed:
            raise python_result
        python_result = (
            0,
            {
                "command": "build",
                "ok": False,
                "skipped": True,
                "reason": "cancelled after TypeScript target failure",
            },
        )
    if isinstance(typescript_result, asyncio.CancelledError):
        py_failed = (
            isinstance(python_result, tuple) and bool(python_result) and int(python_result[0]) != 0
        )
        if not py_failed:
            raise typescript_result
        from jaunt.targets.base import TargetBuildReport

        typescript_result = TargetBuildReport(
            language="ts",
            metadata={"cancelled": True},
        )
    if isinstance(python_result, BaseException):
        raise python_result
    py_code, py_payload = cast("tuple[int, dict[str, object]]", python_result)
    if isinstance(typescript_result, BaseException):
        if not isinstance(typescript_result, Exception):
            raise typescript_result
        error = typescript_result
        if not isinstance(
            error,
            (JauntConfigError, JauntDiscoveryError, JauntGenerationError, KeyError),
        ):
            raise error
        return _mixed_operation_error(
            "build", error, mixed_args, python_code=py_code, python_payload=py_payload
        )
    report = cast("TargetBuildReport", typescript_result)
    ts_payload = build_payload(report)
    py_payload["cost"] = mixed_args._mixed_runtime.summary("py")
    ts_payload["cost"] = mixed_args._mixed_runtime.summary("ts")
    if mixed_args._mixed_npm_skill_metadata:
        ts_payload["npm_skills"] = mixed_args._mixed_npm_skill_metadata
    exit_code = _merge_exit_codes(py_code, report.exit_code)
    payload = _mixed_build_payload("build", py_payload, ts_payload, exit_code=exit_code)
    _emit_mixed_payload(
        payload,
        json_mode=_is_json_mode(args),
        python_payload=py_payload,
        typescript_payload=ts_payload,
    )
    return exit_code


def _cmd_mixed_test(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import test_payload
    from jaunt.typescript.tester import run_test

    target_ids = _mixed_typescript_targets(args)
    if target_ids is not None:
        try:
            _mixed_typescript_preflight(root, cfg, target_ids, for_test=True)
        except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
            return _mixed_preflight_error("test", error, args)
    try:
        _mixed_python_preflight("test", args)
    except (JauntConfigError, JauntDiscoveryError, ImportError, KeyError) as error:
        return _mixed_preflight_error("test", error, args, language="py")
    if target_ids is None:
        from jaunt.targets.base import TargetTestReport

        py_code, py_payload = _capture_python_json(cmd_test, args)
        report = TargetTestReport(language="ts")
        ts_payload = test_payload(report)
        exit_code = _merge_exit_codes(py_code)
        payload = _mixed_build_payload("test", py_payload, ts_payload, exit_code=exit_code)
        _emit_mixed_payload(
            payload,
            json_mode=_is_json_mode(args),
            python_payload=py_payload,
            typescript_payload=ts_payload,
        )
        return exit_code

    try:
        mixed_args = _mixed_runtime_args(args, cfg, command="test")
    except JauntConfigError as error:
        return _mixed_preflight_error("test", error, args)
    _prepare_mixed_typescript_skills(mixed_args, root, cfg)
    _prepare_mixed_repo_map(mixed_args, root, cfg)

    async def run_both() -> tuple[object, object]:
        try:
            return cast(
                "tuple[object, object]",
                await asyncio.gather(
                    asyncio.to_thread(_capture_python_json, cmd_test, mixed_args),
                    mixed_args._mixed_runtime.run_operation(
                        run_test(
                            root,
                            cfg,
                            target_ids=target_ids,
                            no_build=bool(getattr(args, "no_build", False)),
                            no_run=bool(getattr(args, "no_run", False)),
                            no_redact_derived=bool(getattr(args, "no_redact_derived", False)),
                            force=bool(getattr(args, "force", False)),
                            generator=_command_backend(mixed_args, cfg, "ts"),
                            cost_tracker=_command_cost_tracker(mixed_args, cfg, "ts"),
                            response_cache=_typescript_response_cache(mixed_args, root),
                            jobs=getattr(args, "jobs", None),
                            build_instructions=_effective_build_instructions(cfg, args),
                            semantic_gate_enabled=(
                                False if bool(getattr(args, "no_semantic_gate", False)) else None
                            ),
                            semantic_gate_exec=_command_semantic_exec(
                                mixed_args,
                                language="ts",
                                charge_usage=False,
                            ),
                            repo_map_enabled=bool(mixed_args._mixed_repo_map_enabled),
                            repo_map_block_override=mixed_args._mixed_repo_map_block,
                            auto_skills_enabled=False,
                            builtin_skill_names=mixed_args._mixed_builtin_skill_names,
                        ),
                    ),
                    return_exceptions=True,
                ),
            )
        except BaseException:
            mixed_args._mixed_runtime.cancel()
            raise

    python_result, typescript_result = asyncio.run(
        _await_mixed_with_signals(run_both(), mixed_args._mixed_runtime)
    )
    if isinstance(python_result, asyncio.CancelledError):
        ts_failed = (
            isinstance(typescript_result, BaseException)
            or int(getattr(typescript_result, "exit_code", 0)) != 0
        )
        if not ts_failed:
            raise python_result
        python_result = (
            0,
            {
                "command": "test",
                "ok": False,
                "skipped": True,
                "reason": "cancelled after TypeScript target failure",
            },
        )
    if isinstance(typescript_result, asyncio.CancelledError):
        py_failed = (
            isinstance(python_result, tuple) and bool(python_result) and int(python_result[0]) != 0
        )
        if not py_failed:
            raise typescript_result
        from jaunt.targets.base import TargetTestReport

        typescript_result = TargetTestReport(
            language="ts",
            runner={"cancelled": True},
        )
    if isinstance(python_result, BaseException):
        raise python_result
    py_code, py_payload = cast("tuple[int, dict[str, object]]", python_result)
    if isinstance(typescript_result, BaseException):
        if not isinstance(typescript_result, Exception):
            raise typescript_result
        error = typescript_result
        if not isinstance(
            error,
            (JauntConfigError, JauntDiscoveryError, JauntGenerationError, KeyError),
        ):
            raise error
        return _mixed_operation_error(
            "test", error, mixed_args, python_code=py_code, python_payload=py_payload
        )
    report = cast("TargetTestReport", typescript_result)
    ts_payload = test_payload(report)
    py_payload["cost"] = mixed_args._mixed_runtime.summary("py")
    ts_cost = mixed_args._mixed_runtime.summary("ts")
    ts_payload["cost"] = ts_cost
    if mixed_args._mixed_npm_skill_metadata:
        ts_payload["npm_skills"] = mixed_args._mixed_npm_skill_metadata
    exit_code = _merge_exit_codes(py_code, report.exit_code)
    payload = _mixed_build_payload("test", py_payload, ts_payload, exit_code=exit_code)
    _emit_mixed_payload(
        payload,
        json_mode=_is_json_mode(args),
        python_payload=py_payload,
        typescript_payload=ts_payload,
    )
    return exit_code


def _mixed_reconcile_payload(
    python_payload: dict[str, object],
    typescript_payload: dict[str, object],
    *,
    exit_code: int,
) -> dict[str, object]:
    py_reconciled_raw = python_payload.get("reconciled", [])
    py_reconciled = py_reconciled_raw if isinstance(py_reconciled_raw, list) else []
    qualified_reconciled = []
    for item in py_reconciled:
        if not isinstance(item, dict):
            continue
        record = cast("dict[str, object]", item)
        ref = str(record.get("ref", ""))
        qualified_reconciled.append(
            {**record, "ref": ref if ref.startswith("py:") else f"py:{ref}"}
        )
    py_failed = python_payload.get("failed", [])
    ts_diagnostics = typescript_payload.get("diagnostics", [])
    ts_targets = typescript_payload.get("targets", {})
    ts_target = (
        cast("dict[str, object]", ts_targets).get("ts", {}) if isinstance(ts_targets, dict) else {}
    )
    if not isinstance(ts_target, dict):
        ts_target = {}
    ts_usage = typescript_payload.get("usage")
    ts_ok = bool(typescript_payload.get("ok", False))
    ts_target = {
        **ts_target,
        "ok": ts_ok,
        "diagnostics": ts_diagnostics if isinstance(ts_diagnostics, list) else [],
    }
    if isinstance(ts_usage, dict):
        ts_target = {**ts_target, "usage": ts_usage}
    py_target: dict[str, object] = {
        "ok": bool(python_payload.get("ok", False)),
        "skipped": bool(python_payload.get("skipped", False)),
        "reconciled": py_reconciled,
        "failed": py_failed if isinstance(py_failed, list) else [],
    }
    py_cost = python_payload.get("cost")
    if isinstance(py_cost, dict):
        py_target["cost"] = py_cost
    combined_failed = list(cast("list[object]", py_target["failed"]))
    if not ts_ok and isinstance(ts_diagnostics, list):
        combined_failed.append({"target": "ts", "diagnostics": ts_diagnostics})
    payload: dict[str, object] = {
        "schema_version": 2,
        "command": "reconcile",
        "ok": exit_code == 0,
        "reconciled": qualified_reconciled,
        "failed": combined_failed,
        "changed": list(cast("list[object]", typescript_payload.get("changed", []))),
        "diagnostics": ts_diagnostics if isinstance(ts_diagnostics, list) else [],
        "targets": {"py": py_target, "ts": ts_target},
    }
    if isinstance(ts_usage, dict):
        payload["usage"] = ts_usage
    aggregate_cost = _aggregate_cost_payloads(py_cost, ts_usage)
    if aggregate_cost:
        payload["cost"] = aggregate_cost
    strength = typescript_payload.get("strength")
    if isinstance(strength, dict):
        payload["strength"] = strength
    return payload


def _emit_mixed_reconcile_payload(
    payload: dict[str, object],
    *,
    args: argparse.Namespace,
    python_payload: dict[str, object],
    typescript_payload: dict[str, object],
) -> None:
    if _is_json_mode(args):
        _emit_json(payload)
        return
    reconciled = python_payload.get("reconciled", [])
    failed = python_payload.get("failed", [])
    print("Python reconcile:")
    print(f"  reconciled: {len(reconciled) if isinstance(reconciled, list) else 0}")
    print(f"  failed: {len(failed) if isinstance(failed, list) else 0}")
    from jaunt.typescript.cli_bridge import human_lines

    for line in human_lines(typescript_payload):
        print(line)
    changed = typescript_payload.get("changed", [])
    if isinstance(changed, list):
        print(f"  changed: {len(changed)}")
        for path in changed:
            print(f"    - {path}")


def _cmd_mixed_reconcile(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    """Reconcile every selected contract target with TS preflight first."""

    from jaunt.typescript.cli_bridge import lifecycle_payload
    from jaunt.typescript.contracts import LifecycleReport, run_reconcile

    target_ids = _mixed_typescript_targets(args)
    if target_ids is not None:
        try:
            # Contract selectors are path#symbol identities rather than magic
            # module IDs.  Analyze the full TS workspace here; run_reconcile
            # applies the contract-level filter after this mutation-free gate.
            analysis = _mixed_typescript_preflight(
                root,
                cfg,
                (),
                reject_pending_designs=False,
            )
            target_ids = _validated_typescript_contract_targets(analysis, target_ids)
        except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
            return _mixed_preflight_error("reconcile", error, args)
    try:
        _mixed_python_preflight("reconcile", args)
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _mixed_preflight_error("reconcile", error, args, language="py")

    try:
        mixed_args = _mixed_runtime_args(args, cfg, command="reconcile")
    except JauntConfigError as error:
        return _mixed_preflight_error("reconcile", error, args)

    ts_skipped = target_ids is None

    async def run_both() -> tuple[object, object]:
        if target_ids is None:

            async def skipped_typescript() -> LifecycleReport:
                return LifecycleReport(command="reconcile")

            typescript_operation = skipped_typescript()
        else:
            typescript_operation = mixed_args._mixed_runtime.run_operation(
                run_reconcile(
                    root,
                    cfg,
                    target_ids=target_ids,
                    generator=_command_backend(mixed_args, cfg, "ts"),
                    cost_tracker=_command_cost_tracker(mixed_args, cfg, "ts"),
                    response_cache=_typescript_response_cache(mixed_args, root),
                    auto_skills_enabled=_typescript_auto_skills_enabled(mixed_args, cfg),
                    builtin_skill_names=_typescript_builtin_skill_names(mixed_args, cfg),
                )
            )
        try:
            return cast(
                "tuple[object, object]",
                await asyncio.gather(
                    asyncio.to_thread(_capture_python_json, cmd_reconcile, mixed_args),
                    typescript_operation,
                    return_exceptions=True,
                ),
            )
        except BaseException:
            mixed_args._mixed_runtime.cancel()
            raise

    python_result, typescript_result = asyncio.run(
        _await_mixed_with_signals(run_both(), mixed_args._mixed_runtime)
    )
    if isinstance(python_result, asyncio.CancelledError):
        ts_failed = (
            isinstance(typescript_result, BaseException)
            or int(getattr(typescript_result, "exit_code", 0)) != 0
        )
        if not ts_failed:
            raise python_result
        python_result = (
            0,
            {
                "command": "reconcile",
                "ok": False,
                "skipped": True,
                "reason": "cancelled after TypeScript target failure",
            },
        )
    if isinstance(typescript_result, asyncio.CancelledError):
        py_failed = (
            isinstance(python_result, tuple) and bool(python_result) and int(python_result[0]) != 0
        )
        if not py_failed:
            raise typescript_result
        typescript_result = LifecycleReport(command="reconcile")
        ts_skipped = True
    if isinstance(python_result, BaseException):
        raise python_result
    py_code, py_payload = cast("tuple[int, dict[str, object]]", python_result)
    if isinstance(typescript_result, BaseException):
        if not isinstance(typescript_result, Exception):
            raise typescript_result
        error = typescript_result
        if not isinstance(
            error,
            (JauntConfigError, JauntDiscoveryError, JauntGenerationError, KeyError),
        ):
            raise error
        return _mixed_operation_error(
            "reconcile",
            error,
            mixed_args,
            python_code=py_code,
            python_payload=py_payload,
        )
    ts_report = cast("LifecycleReport", typescript_result)
    raw_targets = tuple(str(item) for item in (getattr(args, "target", []) or []))
    if raw_targets and all(item.startswith("ts:") for item in raw_targets):
        py_payload = {
            **py_payload,
            "ok": True,
            "skipped": True,
            "reconciled": [],
            "failed": [],
        }
    py_payload["cost"] = mixed_args._mixed_runtime.summary("py")
    ts_payload = lifecycle_payload(ts_report)
    ts_payload["usage"] = mixed_args._mixed_runtime.summary("ts")
    if ts_skipped:
        raw_targets = ts_payload.get("targets")
        if isinstance(raw_targets, dict) and isinstance(raw_targets.get("ts"), dict):
            cast("dict[str, object]", raw_targets["ts"])["skipped"] = True
    exit_code = _merge_exit_codes(py_code, ts_report.exit_code)
    payload = _mixed_reconcile_payload(py_payload, ts_payload, exit_code=exit_code)
    _emit_mixed_reconcile_payload(
        payload,
        args=args,
        python_payload=py_payload,
        typescript_payload=ts_payload,
    )
    return exit_code


def _cmd_mixed_status(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import status_payload
    from jaunt.typescript.status import run_status

    py_code, py_payload = _capture_python_json(cmd_status, args)
    target_ids = _mixed_typescript_targets(args)
    if target_ids is None:
        from jaunt.targets.base import TargetStatus

        report = TargetStatus(language="ts")
    else:
        try:
            report = asyncio.run(run_status(root, cfg, target_ids=target_ids))
        except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
            return _mixed_operation_error(
                "status", error, args, python_code=py_code, python_payload=py_payload
            )
    ts_payload = status_payload(report)
    exit_code = _merge_exit_codes(py_code)
    py_stale_changes = python_payload_stale = py_payload.get("stale_changes", {})
    if not isinstance(python_payload_stale, dict):
        py_stale_changes = {}
    ts_stale_changes = ts_payload.get("stale_changes", {})
    stale_changes: dict[str, object] = {
        f"py:{key}": value for key, value in cast("dict[str, object]", py_stale_changes).items()
    }
    if isinstance(ts_stale_changes, dict):
        stale_changes.update(ts_stale_changes)
    py_digests_raw = py_payload.get("digests", {})
    py_digests = py_digests_raw if isinstance(py_digests_raw, dict) else {}
    ts_digests_raw = ts_payload.get("digests", {})
    ts_digests = ts_digests_raw if isinstance(ts_digests_raw, dict) else {}
    digests: dict[str, object] = {
        (str(key) if str(key).startswith("py:") else f"py:{key}"): value
        for key, value in py_digests.items()
    }
    digests.update({str(key): value for key, value in ts_digests.items()})
    targets = ts_payload.get("targets", {})
    ts_target = targets.get("ts", {}) if isinstance(targets, dict) else {}
    py_target = {
        key: py_payload.get(key, default)
        for key, default in (
            ("fresh", []),
            ("stale", []),
            ("stale_changes", {}),
            ("digests", {}),
            ("orphans", []),
            ("contracts", []),
        )
    }
    payload: dict[str, object] = {
        "schema_version": 2,
        "command": "status",
        "ok": exit_code == 0,
        "fresh": sorted(
            [
                *_qualify_python_ids(py_payload.get("fresh")),
                *cast("list[str]", ts_payload.get("fresh", [])),
            ]
        ),
        "stale": sorted(
            [
                *_qualify_python_ids(py_payload.get("stale")),
                *cast("list[str]", ts_payload.get("stale", [])),
            ]
        ),
        "stale_changes": stale_changes,
        "digests": digests,
        "unbuilt": list(cast("list[str]", ts_payload.get("unbuilt", []))),
        "invalid": ts_payload.get("invalid", {}),
        "orphans": sorted(
            [
                *cast("list[str]", py_payload.get("orphans", [])),
                *cast("list[str]", ts_payload.get("orphans", [])),
            ]
        ),
        "targets": {"py": py_target, "ts": ts_target},
    }
    _emit_mixed_payload(
        payload,
        json_mode=_is_json_mode(args),
        python_payload=py_payload,
        typescript_payload=ts_payload,
    )
    return exit_code


def _cmd_mixed_check(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import check_payload
    from jaunt.typescript.status import run_check

    py_code, py_payload = _capture_python_json(cmd_check, args)
    target_ids = _mixed_typescript_targets(args)
    if target_ids is None:
        from jaunt.targets.base import TargetCheckReport

        report = TargetCheckReport(language="ts")
    else:
        try:
            report = asyncio.run(
                run_check(
                    root,
                    cfg,
                    target_ids=target_ids,
                    magic_only=bool(getattr(args, "magic_only", False)),
                    contracts_only=bool(getattr(args, "contracts_only", False)),
                )
            )
        except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
            return _mixed_operation_error(
                "check", error, args, python_code=py_code, python_payload=py_payload
            )
    ts_payload = check_payload(report)
    exit_code = _merge_exit_codes(py_code, report.exit_code)
    py_magic = py_payload.get("magic", {})
    ts_magic_container = ts_payload.get("magic", {})
    ts_magic = ts_magic_container.get("ts", {}) if isinstance(ts_magic_container, dict) else {}
    py_diagnostics = py_payload.get("diagnostics", [])
    ts_diagnostics = ts_payload.get("diagnostics", [])
    combined_diagnostics = [
        *(py_diagnostics if isinstance(py_diagnostics, list) else []),
        *(ts_diagnostics if isinstance(ts_diagnostics, list) else []),
    ]
    payload: dict[str, object] = {
        "schema_version": 2,
        "command": "check",
        "ok": exit_code == 0,
        "blocked": [
            *cast("list[object]", py_payload.get("blocked", [])),
            *cast("list[object]", ts_payload.get("blocked", [])),
        ],
        "checked": [
            *cast("list[object]", py_payload.get("checked", [])),
            *cast("list[object]", ts_payload.get("checked", [])),
        ],
        "orphans": [
            *cast("list[str]", py_payload.get("orphans", [])),
            *cast("list[str]", ts_payload.get("orphans", [])),
        ],
        "diagnostics": combined_diagnostics,
        "magic": {"py": py_magic, "ts": ts_magic},
        "targets": {
            "py": {"magic": py_magic, "diagnostics": py_diagnostics},
            "ts": {"magic": ts_magic, "diagnostics": ts_diagnostics},
        },
    }
    _emit_mixed_payload(
        payload,
        json_mode=_is_json_mode(args),
        python_payload=py_payload,
        typescript_payload=ts_payload,
    )
    return exit_code


def _cmd_mixed_specs(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import specs_payload
    from jaunt.typescript.status import run_specs

    py_code, py_payload = _capture_python_json(cmd_specs, args)
    target_ids = _mixed_typescript_targets(args)
    module_filter = getattr(args, "module", None)
    if isinstance(module_filter, str):
        if module_filter.startswith("ts:") and target_ids is not None:
            target_ids = (*target_ids, module_filter)
        else:
            target_ids = None
    if target_ids is None:
        from jaunt.targets.base import TargetWorkspace

        report = TargetWorkspace(language="ts")
    else:
        try:
            report = asyncio.run(run_specs(root, cfg, target_ids=target_ids))
        except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
            return _mixed_operation_error(
                "specs", error, args, python_code=py_code, python_payload=py_payload
            )
    ts_payload = specs_payload(report)
    exit_code = _merge_exit_codes(py_code)
    py_specs = py_payload.get("specs", [])
    ts_specs = ts_payload.get("specs", [])
    payload: dict[str, object] = {
        "schema_version": 2,
        "command": "specs",
        "ok": exit_code == 0,
        "specs": [
            *(
                [{**item, "language": "py"} for item in py_specs if isinstance(item, dict)]
                if isinstance(py_specs, list)
                else []
            ),
            *(
                [{**item, "language": "ts"} for item in ts_specs if isinstance(item, dict)]
                if isinstance(ts_specs, list)
                else []
            ),
        ],
        "dependency_graph": {
            "py": py_payload.get("dependency_graph", {}),
            "ts": ts_payload.get("dependency_graph", {}),
        },
        "targets": {
            "py": {"specs": py_specs},
            "ts": ts_payload.get("targets", {}).get("ts", {})
            if isinstance(ts_payload.get("targets"), dict)
            else {},
        },
    }
    _emit_mixed_payload(
        payload,
        json_mode=_is_json_mode(args),
        python_payload=py_payload,
        typescript_payload=ts_payload,
    )
    return exit_code


def _cmd_mixed_clean(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    from jaunt.typescript.cli_bridge import clean_payload
    from jaunt.typescript.status import run_clean

    try:
        target_ids = _typescript_target_ids(args)
        _mixed_typescript_preflight(root, cfg, target_ids, reject_pending_designs=False)
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _mixed_preflight_error("clean", error, args)
    if target_ids:
        py_code = EXIT_OK
        py_payload: dict[str, object] = {
            "command": "clean",
            "ok": True,
            "removed": [],
            "would_remove": [],
        }
    else:
        try:
            _mixed_python_preflight("clean", args)
        except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
            return _mixed_preflight_error("clean", error, args, language="py")
        py_code, py_payload = _capture_python_json(cmd_clean, args)
    try:
        report = asyncio.run(
            run_clean(
                root,
                cfg,
                target_ids=target_ids,
                orphans_only=bool(getattr(args, "orphans", False)),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
        )
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _mixed_operation_error(
            "clean", error, args, python_code=py_code, python_payload=py_payload
        )
    ts_payload = clean_payload(report)
    exit_code = _merge_exit_codes(py_code, report.exit_code)
    payload: dict[str, object] = {
        "schema_version": 2,
        "command": "clean",
        "ok": exit_code == 0,
        "removed": [
            *cast("list[str]", py_payload.get("removed", [])),
            *cast("list[str]", ts_payload.get("removed", [])),
        ],
        "would_remove": [
            *cast("list[str]", py_payload.get("would_remove", [])),
            *cast("list[str]", ts_payload.get("would_remove", [])),
        ],
        "targets": {
            "py": py_payload,
            "ts": ts_payload.get("targets", {}).get("ts", {})
            if isinstance(ts_payload.get("targets"), dict)
            else {},
        },
    }
    _emit_mixed_payload(
        payload,
        json_mode=_is_json_mode(args),
        python_payload=py_payload,
        typescript_payload=ts_payload,
    )
    return exit_code


def _effective_build_instructions(cfg: JauntConfig, args: argparse.Namespace) -> list[str]:
    configured = list(cfg.build.instructions)
    cli_values = [value.strip() for value in list(getattr(args, "instructions", []) or [])]
    return [value for value in [*configured, *cli_values] if value]


def _effective_include_target_tests(cfg: JauntConfig, args: argparse.Namespace) -> bool:
    override = getattr(args, "include_target_tests", None)
    if override is None:
        return bool(cfg.build.include_target_tests)
    return bool(override)


def _discover_test_spec_modules(*, root: Path, cfg: JauntConfig) -> tuple[list[Path], list[str]]:
    from jaunt import discovery
    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    existing_test_dirs = [route.root for route in workspace.test_roots]
    modules_set: set[str] = set()
    for route in workspace.test_roots:
        mods = discovery.discover_modules(
            roots=[route.root],
            exclude=[],
            generated_dir=cfg.paths.generated_dir,
            module_prefix=route.module_prefix,
        )
        modules_set.update(mods)
    return existing_test_dirs, sorted(modules_set)


def _discover_contract_specs(*, root: Path, cfg: JauntConfig) -> dict[SpecRef, SpecEntry]:
    from jaunt import discovery, registry
    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    source_dirs = list(workspace.source_roots)
    _prepend_sys_path([*source_dirs, root])
    modules = [route.module for route in workspace.modules]
    discovery.prepare_import_environment(
        module_names=modules, roots=[d for d in source_dirs if d.exists()]
    )
    discovery.import_and_collect(modules, kind="contract")
    return dict(registry.get_contract_registry())


def _resolve_contract_source_file(*, root: Path, cfg: JauntConfig, module: str) -> Path:
    from jaunt.workspace import resolve_module_source

    return resolve_module_source(root, cfg, module)


def _contract_owner_context(*, root: Path, cfg: JauntConfig, module: str) -> tuple[Path, list[str]]:
    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    owner = workspace.route_for(module).owner_dir
    return owner, [str(path) for path in workspace.source_roots]


def _build_backend(cfg: JauntConfig):
    from jaunt.generate.codex_backend import CodexBackend

    return CodexBackend(cfg.codex, cfg.llm, cfg.prompts)


def _command_backend(
    args: argparse.Namespace,
    cfg: JauntConfig,
    language: Literal["py", "ts"],
) -> GeneratorBackend:
    """Return the normal backend or the command's shared mixed-target wrapper."""

    runtime = getattr(args, "_mixed_runtime", None)
    if runtime is None:
        return _build_backend(cfg)
    return runtime.backend(language, lambda: _build_backend(cfg))


def _command_cost_tracker(
    args: argparse.Namespace,
    cfg: JauntConfig,
    language: Literal["py", "ts"],
) -> CostTracker:
    """Return a phase tracker charged to the mixed command's shared ledger."""

    runtime = getattr(args, "_mixed_runtime", None)
    if runtime is not None:
        return runtime.cost_tracker(language)
    from jaunt.cost import CostTracker

    return CostTracker(max_cost=cfg.llm.max_cost_per_build)


def _command_cost_summary(
    args: argparse.Namespace,
    language: Literal["py", "ts"],
    tracker: CostTracker,
) -> dict[str, object]:
    runtime = getattr(args, "_mixed_runtime", None)
    if runtime is not None:
        return cast("dict[str, object]", runtime.summary(language))
    return tracker.summary_dict()


def _check_shared_command_budget(
    args: argparse.Namespace,
    language: Literal["py", "ts"],
) -> None:
    runtime = getattr(args, "_mixed_runtime", None)
    if runtime is not None:
        runtime.cost_tracker(language).check_budget()


def _command_semantic_exec(
    args: argparse.Namespace,
    *,
    language: Literal["py", "ts"] = "py",
    charge_usage: bool = True,
):
    """Gate direct semantic-judge calls under the mixed model-call limit."""

    runtime = getattr(args, "_mixed_runtime", None)
    if runtime is None:
        return None

    from jaunt.generate.codex_backend import run_codex_exec

    tracker = runtime.cost_tracker(language)

    async def run_limited(**kwargs):
        result = await runtime.run_call(run_codex_exec, **kwargs)
        if charge_usage:
            usage_input = getattr(result, "usage_input", None)
            usage_output = getattr(result, "usage_output", None)
            if isinstance(usage_input, int) and isinstance(usage_output, int):
                from jaunt.generate.base import TokenUsage

                tracker.record(
                    "semantic-gate",
                    TokenUsage(
                        prompt_tokens=usage_input,
                        completion_tokens=usage_output,
                        model=str(kwargs.get("model", "")),
                        provider="codex",
                        cached_prompt_tokens=getattr(result, "usage_cached", None) or 0,
                    ),
                )
        return result

    return run_limited


def _is_json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json_output", False))


def _resolve_progress_mode(
    args: argparse.Namespace, *, json_mode: bool
) -> Literal["rich", "plain"] | None:
    if bool(getattr(args, "no_progress", False)):
        return None

    requested = str(getattr(args, "progress", "auto") or "auto")
    if requested == "none":
        return None
    if json_mode and requested == "auto":
        return None
    if requested == "auto":
        return "rich" if sys.stderr.isatty() else "plain"
    return cast(Literal["rich", "plain"], requested)


def _make_progress(
    args: argparse.Namespace, *, label: str, total: int, json_mode: bool
) -> ProgressBar | None:
    if total == 0:
        return None
    mode = _resolve_progress_mode(args, json_mode=json_mode)
    if mode is None:
        return None
    return ProgressBar(label=label, total=total, enabled=True, stream=sys.stderr, mode=mode)


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _print_error(e: BaseException) -> None:
    _eprint(format_error_with_hint(e))


def _emit_json(data: dict[str, object]) -> None:
    """Write structured JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


def _job_state_label(job: JobRecord) -> str:
    return f"{job.state} — {job.phase}" if job.phase else job.state


def cmd_log(args: argparse.Namespace) -> int:
    from jaunt import journal

    root = Path(args.root).resolve()
    lines = journal.read_lines(root, limit=args.lines, module=args.module)
    if _is_json_mode(args):
        _emit_json({"command": "log", "ok": True, "lines": lines})
        return EXIT_OK
    if not lines:
        print("No journal entries (no JAUNT_LOG file, or it is empty).")
        return EXIT_OK
    for line in lines:
        print(line)
    return EXIT_OK


def cmd_daemon(args: argparse.Namespace) -> int:
    import signal

    from jaunt import daemon as daemon_mod
    from jaunt import jobs as jobs_mod

    root = Path(args.root).resolve()
    if args.daemon_command == "stop":
        try:
            running, pid = daemon_mod.probe_lock(root)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return EXIT_CONFIG_OR_DISCOVERY
        if not running:
            print("Daemon not running.")
            return EXIT_OK
        if pid is None:
            print("Daemon lockfile is locked but has no readable pid; refusing to signal.")
            return EXIT_OK
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to daemon (pid {pid}).")
        return EXIT_OK

    if args.daemon_command == "status":
        try:
            running, pid = daemon_mod.probe_lock(root)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return EXIT_CONFIG_OR_DISCOVERY
        records = jobs_mod.list_jobs(root)
        if _is_json_mode(args):
            _emit_json(
                {
                    "command": "daemon-status",
                    "ok": True,
                    "running": running,
                    "pid": pid,
                    "jobs": [
                        {
                            "id": job.id,
                            "module": job.module,
                            "state": job.state,
                            "phase": job.phase,
                            **(
                                {"language": job.language, "artifact_key": job.key}
                                if job.language != "py"
                                else {}
                            ),
                        }
                        for job in records
                    ],
                }
            )
        else:
            if running and pid is not None:
                status = f"running (pid {pid})"
            elif running:
                status = "running (pid unknown)"
            else:
                status = "stopped"
            print(f"Daemon: {status}")
            try:
                from jaunt.config import load_config

                landing_mode = (
                    "auto-commit" if load_config(root=root).daemon.auto_commit else "propose-only"
                )
            except Exception:
                landing_mode = "propose-only"
            print(f"landing: {landing_mode}")
            for job in records[-10:]:
                label = job.key if job.language != "py" else job.module
                print(f"- {job.id} {label}: {_job_state_label(job)}")
        return EXIT_OK

    if os.environ.get(daemon_mod.DISABLE_ENV):
        print(f"{daemon_mod.DISABLE_ENV} is set; refusing to start.", file=sys.stderr)
        return EXIT_CONFIG_OR_DISCOVERY

    if not daemon_mod.jaunt_dir_ignored(root):
        print(
            "error: .jaunt/ must be gitignored before running the daemon "
            "(its cache and job state would otherwise trip the landing allowlist). "
            "Add '.jaunt/' to .gitignore.",
            file=sys.stderr,
        )
        return EXIT_CONFIG_OR_DISCOVERY

    try:
        lock = daemon_mod.acquire_lock(root)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_OR_DISCOVERY
    if lock is None:
        print("Daemon already running.", file=sys.stderr)
        return EXIT_CONFIG_OR_DISCOVERY

    try:
        daemon_mod.run_daemon(root)
        return EXIT_OK
    finally:
        daemon_mod.release_lock(lock)


def _jobs_magic_status(root: Path, args: argparse.Namespace, target: tuple[str, ...]):
    from jaunt.config import load_config

    cfg = load_config(root=root)
    include_target_tests = _effective_include_target_tests(cfg, args)
    build_instructions = _effective_build_instructions(cfg, args)
    source_dirs = [root / sr for sr in cfg.paths.source_roots]
    _prepend_sys_path([*source_dirs, root])
    infer_default = bool(cfg.build.infer_deps)
    return compute_magic_status(
        root=root,
        cfg=cfg,
        source_dirs=source_dirs,
        build_instructions=build_instructions,
        include_target_tests=include_target_tests,
        infer_deps=infer_default,
        force=False,
        target=target,
    )


def _module_current_digest(root: Path, args: argparse.Namespace, module: str) -> str | None:
    mstatus = _jobs_magic_status(root, args, (module,))
    return mstatus.digests.get(module)


def _job_current_digest(
    root: Path,
    args: argparse.Namespace,
    job: JobRecord,
) -> str | None:
    if job.language == "py":
        return _module_current_digest(root, args, job.module)
    from jaunt.daemon import CliRunner, ProbeError

    try:
        _stale, digests = CliRunner().probe(root)
    except ProbeError as error:
        raise JauntConfigError(f"TypeScript proposal freshness probe failed: {error}") from error
    return digests.get(job.key)


def _jobs_would_rebuild(root: Path, args: argparse.Namespace) -> dict[str, str]:
    from jaunt.config import load_config

    cfg = load_config(root=root)
    if cfg.version == 2:
        from jaunt.daemon import CliRunner

        stale, _digests = CliRunner().probe(root)
        return dict(sorted(stale.items()))
    mstatus = _jobs_magic_status(root, args, ())
    return {mod: mstatus.stale_changes.get(mod, "structural") for mod in sorted(mstatus.stale)}


class _WaitPrinter:
    def __init__(self, mode: Literal["rich", "plain"] | None) -> None:
        self.mode = mode
        self.enabled = mode is not None

    def job(self, job: JobRecord) -> None:
        if not self.enabled:
            return
        self._write(_wait_line(job) + "\n")

    def _write(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:
            self.enabled = False


def _wait_line(job: JobRecord) -> str:
    status = job.state
    if job.state in {"failed", "parked"} and job.error:
        status = f"{job.state}: {job.error.splitlines()[0]}"
    elif job.phase:
        status = f"{job.state} — {job.phase}"
    label = job.key if job.language != "py" else job.module
    return f"[wait] {job.id} {label}: {status}"


def _wait_payload(job: JobRecord) -> dict[str, str]:
    payload = {
        "id": job.id,
        "module": job.module,
        "state": job.state,
        "phase": job.phase,
        "error": job.error,
    }
    if job.language != "py":
        payload.update({"language": job.language, "artifact_key": job.key})
    return payload


def _emit_jobs_wait_json(
    *,
    json_mode: bool,
    ok: bool,
    timed_out: bool,
    watched: dict[str, JobRecord],
) -> None:
    if not json_mode:
        return
    jobs_payload = [
        _wait_payload(job) for job in sorted(watched.values(), key=lambda j: (j.created, j.id))
    ]
    _emit_json(
        {
            "command": "jobs",
            "action": "wait",
            "ok": ok,
            "timed_out": timed_out,
            "jobs": jobs_payload,
        }
    )


def _jobs_wait_settle_seconds(root: Path, args: argparse.Namespace) -> float:
    if getattr(args, "settle", None) is not None:
        return float(args.settle)

    from jaunt.config import load_config

    cfg = load_config(root=root)
    return float(cfg.daemon.poll_interval) * 2


def _jobs_wait_sleep(
    *,
    now: float,
    deadline: float | None,
    sleep: Callable[[float], None],
) -> None:
    delay = _JOBS_WAIT_POLL_SECONDS
    if deadline is not None:
        delay = min(delay, max(0.0, deadline - now))
    if delay > 0:
        sleep(delay)


def _jobs_wait_daemon_running(root: Path) -> bool:
    from jaunt import daemon as daemon_mod

    running, _pid = daemon_mod.probe_lock(root)
    return running


def _jobs_wait_result_code(watched: dict[str, JobRecord]) -> int:
    if any(job.state in {"failed", "parked"} for job in watched.values()):
        return EXIT_PYTEST_FAILURE
    return EXIT_OK


def _cmd_jobs_wait(
    args: argparse.Namespace,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    from jaunt import jobs as jobs_mod

    from jaunt.config import find_project_root

    json_mode = _is_json_mode(args)
    raw_root = getattr(args, "root", None)
    # The jobs parser defaults --root to "."; discover the enclosing project
    # like build/test do so `jobs wait` works from a subdirectory (and reads
    # the right [daemon] poll_interval for the settle default).
    if raw_root in (None, "."):
        root = find_project_root(Path.cwd())
    else:
        root = Path(raw_root).resolve()
    target_id = getattr(args, "job_id", None)
    printer = _WaitPrinter(_resolve_progress_mode(args, json_mode=json_mode))
    watched: dict[str, JobRecord] = {}
    seen_status: dict[str, tuple[str, str, str]] = {}
    wait_started_at = time.time()

    def remember(job: JobRecord) -> None:
        watched[job.id] = job
        key = (
            job.state,
            job.phase,
            job.error if job.state in {"failed", "parked"} else "",
        )
        if seen_status.get(job.id) != key:
            seen_status[job.id] = key
            printer.job(job)

    if target_id is not None:
        initial = jobs_mod.load_job(root, target_id)
        if initial is None:
            _eprint(f"error: job not found: {target_id}")
            return EXIT_CONFIG_OR_DISCOVERY
        remember(initial)
        if initial.state not in jobs_mod.ACTIVE_STATES:
            rc = _jobs_wait_result_code(watched)
            _emit_jobs_wait_json(
                json_mode=json_mode,
                ok=rc == EXIT_OK,
                timed_out=False,
                watched=watched,
            )
            return rc
    try:
        settle = _jobs_wait_settle_seconds(root, args) if target_id is None else 0.0
    except JauntConfigError as e:
        _print_error(e)
        _emit_jobs_wait_json(json_mode=json_mode, ok=False, timed_out=False, watched=watched)
        return EXIT_CONFIG_OR_DISCOVERY

    deadline = None
    if getattr(args, "timeout", None) is not None:
        deadline = clock() + float(args.timeout)
    idle_since: float | None = None

    while True:
        now = clock()

        if target_id is not None:
            current = jobs_mod.load_job(root, target_id)
            if current is None:
                _eprint(f"error: job disappeared while waiting: {target_id}")
                _emit_jobs_wait_json(
                    json_mode=json_mode,
                    ok=False,
                    timed_out=False,
                    watched=watched,
                )
                return EXIT_CONFIG_OR_DISCOVERY
            remember(current)
            if current.state not in jobs_mod.ACTIVE_STATES:
                rc = _jobs_wait_result_code(watched)
                _emit_jobs_wait_json(
                    json_mode=json_mode,
                    ok=rc == EXIT_OK,
                    timed_out=False,
                    watched=watched,
                )
                return rc
            try:
                daemon_running = _jobs_wait_daemon_running(root)
            except RuntimeError as e:
                _eprint(str(e))
                _emit_jobs_wait_json(
                    json_mode=json_mode,
                    ok=False,
                    timed_out=False,
                    watched=watched,
                )
                return EXIT_CONFIG_OR_DISCOVERY
            if not daemon_running:
                _eprint(f"daemon died mid-job; active job record is stale: {target_id}")
                _emit_jobs_wait_json(
                    json_mode=json_mode,
                    ok=False,
                    timed_out=False,
                    watched=watched,
                )
                return EXIT_CONFIG_OR_DISCOVERY
        else:
            records = jobs_mod.list_jobs(root)
            records_by_id = {job.id: job for job in records}
            active = [job for job in records if job.state in jobs_mod.ACTIVE_STATES]
            # Look back one settle window: a job the daemon finished in the
            # instant before wait started (fast failure right after a commit)
            # still belongs to this wait.
            cutoff = wait_started_at - settle
            recent = [job for job in records if job.created >= cutoff or job.updated >= cutoff]
            for job in active:
                remember(job)
            for job in recent:
                remember(job)
            for job_id in list(watched):
                current = records_by_id.get(job_id)
                if current is not None:
                    remember(current)

            if active:
                idle_since = None
                try:
                    daemon_running = _jobs_wait_daemon_running(root)
                except RuntimeError as e:
                    _eprint(str(e))
                    _emit_jobs_wait_json(
                        json_mode=json_mode,
                        ok=False,
                        timed_out=False,
                        watched=watched,
                    )
                    return EXIT_CONFIG_OR_DISCOVERY
                if not daemon_running:
                    _eprint("daemon died mid-job; active job records are stale")
                    _emit_jobs_wait_json(
                        json_mode=json_mode,
                        ok=False,
                        timed_out=False,
                        watched=watched,
                    )
                    return EXIT_CONFIG_OR_DISCOVERY
            else:
                if not records and not watched:
                    try:
                        daemon_running = _jobs_wait_daemon_running(root)
                    except RuntimeError as e:
                        _eprint(str(e))
                        _emit_jobs_wait_json(
                            json_mode=json_mode,
                            ok=False,
                            timed_out=False,
                            watched=watched,
                        )
                        return EXIT_CONFIG_OR_DISCOVERY
                    if not daemon_running:
                        _emit_jobs_wait_json(
                            json_mode=json_mode,
                            ok=True,
                            timed_out=False,
                            watched=watched,
                        )
                        return EXIT_OK
                elif records and not watched:
                    try:
                        daemon_running = _jobs_wait_daemon_running(root)
                    except RuntimeError as e:
                        _eprint(str(e))
                        _emit_jobs_wait_json(
                            json_mode=json_mode,
                            ok=False,
                            timed_out=False,
                            watched=watched,
                        )
                        return EXIT_CONFIG_OR_DISCOVERY
                    if not daemon_running:
                        _eprint("daemon not running; run `jaunt daemon start` to enqueue new jobs")
                        _emit_jobs_wait_json(
                            json_mode=json_mode,
                            ok=False,
                            timed_out=False,
                            watched=watched,
                        )
                        return EXIT_CONFIG_OR_DISCOVERY

                if idle_since is None:
                    idle_since = now
                if settle <= 0 or now - idle_since >= settle:
                    rc = _jobs_wait_result_code(watched)
                    _emit_jobs_wait_json(
                        json_mode=json_mode,
                        ok=rc == EXIT_OK,
                        timed_out=False,
                        watched=watched,
                    )
                    return rc

        now = clock()
        if deadline is not None and now >= deadline:
            _eprint("timed out waiting for jobs")
            _emit_jobs_wait_json(
                json_mode=json_mode,
                ok=False,
                timed_out=True,
                watched=watched,
            )
            return EXIT_TIMEOUT
        _jobs_wait_sleep(now=now, deadline=deadline, sleep=sleep)


def _cmd_jobs_list(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from jaunt import jobs as jobs_mod

    json_mode = _is_json_mode(args)
    root = Path(args.root).resolve()
    try:
        records = jobs_mod.list_jobs(root)
        would_rebuild = _jobs_would_rebuild(root, args)
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "jobs", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    if json_mode:
        _emit_json(
            {
                "command": "jobs",
                "ok": True,
                "jobs": [asdict(job) for job in records],
                "would_rebuild": would_rebuild,
            }
        )
        return EXIT_OK

    if not records:
        print("No job records.")
    for job in records:
        print(f"- {job.id} {_job_artifact_label(job)}: {_job_state_label(job)}")
        if job.battery:
            print(f"  battery {job.battery}")
        if job.error:
            print(f"  {job.error.splitlines()[0]}")
    for module, change in would_rebuild.items():
        print(f"would rebuild: {module} ({change})")
    return EXIT_OK


def _cmd_jobs_show(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from jaunt import jobs as jobs_mod

    root = Path(args.root).resolve()
    job = jobs_mod.load_job(root, args.job_id)
    if job is None:
        _eprint(f"error: job not found: {args.job_id}")
        return EXIT_CONFIG_OR_DISCOVERY

    if _is_json_mode(args):
        _emit_json({"command": "jobs-show", "ok": True, "job": asdict(job)})
        return EXIT_OK

    for key, value in asdict(job).items():
        if key == "state":
            print(f"state: {_job_state_label(job)}")
        elif key == "phase" and job.phase:
            continue
        elif key == "advisories":
            if not value:
                continue
            try:
                items = json.loads(value)
            except json.JSONDecodeError:
                items = []
            if not items:
                continue
            print("advisories:")
            for item in items:
                print(f"  - {item}")
        else:
            print(f"{key}: {value}")
    if args.full and job.detail_log:
        detail = Path(job.detail_log)
        if detail.exists():
            print(detail.read_text(encoding="utf-8"), end="")
    return EXIT_OK


def _job_artifact_label(job: JobRecord) -> str:
    return job.key if job.language != "py" else job.module


def _revalidate_typescript_job_patch(
    root: Path,
    job: JobRecord,
    patch: str,
    patch_paths: Sequence[str],
) -> tuple[bool, str]:
    """Gate the exact TS proposal in a disposable worktree before landing."""

    if job.language != "ts":
        return True, ""
    from jaunt import daemon as daemon_mod
    from jaunt import landing
    from jaunt.config import load_config

    cfg = load_config(root=root)
    runner = daemon_mod.CliRunner()

    def validate(worktree: Path) -> tuple[bool, str]:
        with daemon_mod._typescript_tool_link(root, worktree, cfg, enabled=True):
            outcome = runner.gate(
                worktree,
                job.module,
                language=job.language,
                artifact_key=job.key,
            )
        return outcome.ok, outcome.detail

    try:
        return landing.validate_patch(
            root,
            patch,
            patch_paths=patch_paths,
            validator=validate,
        )
    except (landing.LandingError, JauntConfigError) as error:
        return False, str(error).splitlines()[0][:200]


def _cmd_jobs_retry(args: argparse.Namespace) -> int:
    from jaunt import jobs as jobs_mod
    from jaunt import landing

    root = Path(args.root).resolve()
    job = jobs_mod.load_job(root, args.job_id)
    if job is None:
        _eprint(f"error: job not found: {args.job_id}")
        return EXIT_CONFIG_OR_DISCOVERY
    if job.state != jobs_mod.PARKED:
        _eprint(f"error: job {job.id} is {job.state}; only parked jobs can be retried")
        return EXIT_CONFIG_OR_DISCOVERY
    if not job.patch_paths:
        _eprint(f"error: parked job {job.id} has no patch paths")
        return EXIT_CONFIG_OR_DISCOVERY

    try:
        patch_paths_raw = json.loads(job.patch_paths)
    except json.JSONDecodeError:
        _eprint(f"error: parked job {job.id} has invalid patch paths")
        return EXIT_CONFIG_OR_DISCOVERY
    if not isinstance(patch_paths_raw, list) or not all(
        isinstance(path, str) for path in patch_paths_raw
    ):
        _eprint(f"error: parked job {job.id} has invalid patch paths")
        return EXIT_CONFIG_OR_DISCOVERY

    patch_file = jobs_mod.jobs_dir(root) / f"{job.id}.patch"
    if not patch_file.exists():
        _eprint(f"error: parked job {job.id} is missing patch file")
        return EXIT_CONFIG_OR_DISCOVERY

    patch = patch_file.read_text(encoding="utf-8")
    if not args.force:
        try:
            current_digest = _job_current_digest(root, args, job)
        except JauntConfigError as error:
            _eprint(f"error: {error}")
            return EXIT_CONFIG_OR_DISCOVERY
        if current_digest is None or current_digest != job.spec_digest:
            _eprint(
                f"error: {_job_artifact_label(job)} spec changed since this job parked; "
                "the daemon will rebuild it -- use --force to land anyway"
            )
            return EXIT_PYTEST_FAILURE

    valid, validation_detail = _revalidate_typescript_job_patch(
        root,
        job,
        patch,
        patch_paths_raw,
    )
    if not valid:
        _eprint(
            f"error: refusing to land {job.id}; target-scoped TypeScript check failed: "
            f"{validation_detail or 'jaunt check failed'}"
        )
        return EXIT_PYTEST_FAILURE

    try:
        expected_head = landing.git_out(root, "rev-parse", "HEAD").strip()
        sha = landing.land(
            root,
            patch,
            patch_paths=patch_paths_raw,
            message=landing.build_commit_message(
                _job_artifact_label(job),
                "retry landing",
                job.id,
                job.spec_digest,
            ),
            expected_branch=job.branch,
            expected_head=expected_head,
        )
    except landing.LandingError as e:
        _eprint(str(e))
        return EXIT_PYTEST_FAILURE

    if not sha or sha == landing.HEAD_MOVED:
        _eprint(f"parked: retry could not land job {job.id}")
        return EXIT_PYTEST_FAILURE

    jobs_mod.mark(root, job, jobs_mod.LANDED, landed_commit=sha, phase="")
    print(sha)
    return EXIT_OK


def _land_one_proposal(
    root: Path, args: argparse.Namespace, job: JobRecord, *, json_mode: bool = False
) -> tuple[int, bool, dict[str, object]]:
    from jaunt import jobs as jobs_mod
    from jaunt import journal as journal_mod
    from jaunt import landing

    def _fail(
        code: int, msg: str, *, aborted: bool = False, state: str | None = None
    ) -> tuple[int, bool, dict[str, object]]:
        if not json_mode:
            _eprint(msg)
        return (
            code,
            aborted,
            {
                "job_id": job.id,
                "ok": False,
                "state": state or job.state,
                "sha": None,
                "error": msg,
            },
        )

    def _landed(sha: str) -> tuple[int, bool, dict[str, object]]:
        if not json_mode:
            print(sha)
        return (
            EXIT_OK,
            False,
            {
                "job_id": job.id,
                "ok": True,
                "state": jobs_mod.LANDED,
                "sha": sha,
                "error": None,
            },
        )

    if not job.patch_paths:
        return _fail(EXIT_CONFIG_OR_DISCOVERY, f"error: proposed job {job.id} has no patch paths")

    try:
        patch_paths_raw = json.loads(job.patch_paths)
    except json.JSONDecodeError:
        return _fail(
            EXIT_CONFIG_OR_DISCOVERY, f"error: proposed job {job.id} has invalid patch paths"
        )
    if (
        not isinstance(patch_paths_raw, list)
        or not patch_paths_raw
        or not all(isinstance(path, str) for path in patch_paths_raw)
    ):
        return _fail(
            EXIT_CONFIG_OR_DISCOVERY, f"error: proposed job {job.id} has invalid patch paths"
        )

    patch_file = jobs_mod.jobs_dir(root) / f"{job.id}.patch"
    if not patch_file.exists():
        return _fail(
            EXIT_CONFIG_OR_DISCOVERY, f"error: proposed job {job.id} is missing patch file"
        )

    patch = patch_file.read_text(encoding="utf-8")
    try:
        current_digest = _job_current_digest(root, args, job)
    except JauntConfigError as error:
        return _fail(EXIT_CONFIG_OR_DISCOVERY, f"error: {error}", aborted=True)
    if current_digest is None or current_digest != job.spec_digest:
        jobs_mod.mark(root, job, jobs_mod.SUPERSEDED)
        return _fail(
            EXIT_PYTEST_FAILURE,
            f"superseded: {_job_artifact_label(job)} spec moved since generation; "
            "the daemon will propose a fresh build",
            state=jobs_mod.SUPERSEDED,
        )

    valid, validation_detail = _revalidate_typescript_job_patch(
        root,
        job,
        patch,
        patch_paths_raw,
    )
    if not valid:
        return _fail(
            EXIT_PYTEST_FAILURE,
            f"error: refusing to land {job.id}; target-scoped TypeScript check failed: "
            f"{validation_detail or 'jaunt check failed'}",
        )

    try:
        current_branch = landing.git_out(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current_branch != job.branch:
            return _fail(
                EXIT_PYTEST_FAILURE,
                f"error: on branch {current_branch}; proposal was generated on {job.branch}",
            )
        dirty = landing.git_out(root, "status", "--porcelain", "--", *patch_paths_raw).strip()
    except landing.LandingError as e:
        return _fail(EXIT_PYTEST_FAILURE, str(e), aborted=True)
    if dirty:
        return _fail(
            EXIT_PYTEST_FAILURE,
            f"error: refusing to land {job.id}; working tree has changes to: "
            f"{' '.join(patch_paths_raw)}",
        )

    def truncate_journal(path: Path, size: int) -> None:
        with open(path, "r+", encoding="utf-8") as f:
            f.truncate(size)

    journal_path = root / journal_mod.JOURNAL_FILE
    journal_opted_in = journal_path.exists()
    # Mirror the daemon auto-commit guard: never sweep unrelated user edits to
    # JAUNT_LOG into the provenance commit. Daemon-authored additions are safe.
    if journal_opted_in and journal_mod.user_dirty(root):
        return _fail(
            EXIT_PYTEST_FAILURE,
            f"error: refusing to land {job.id}; {journal_mod.JOURNAL_FILE} has uncommitted "
            "edits -- commit or stash them first",
        )
    snapshot_len = 0
    extra_paths: tuple[str, ...] = ()
    if journal_opted_in:
        snapshot_len = journal_path.stat().st_size
        journal_mod.append_events(
            root,
            [
                journal_mod.JournalEvent(
                    "refreeze" if job.refrozen else "build",
                    _job_artifact_label(job),
                    f"{job.cause or 'spec change'}; battery {job.battery or '-'}",
                    job.id,
                )
            ],
        )
        extra_paths = (journal_mod.JOURNAL_FILE,)

    try:
        expected_head = landing.git_out(root, "rev-parse", "HEAD").strip()
        sha = landing.land(
            root,
            patch,
            patch_paths=patch_paths_raw,
            message=landing.build_commit_message(
                _job_artifact_label(job),
                job.cause or "spec change",
                job.id,
                job.spec_digest,
            ),
            expected_branch=job.branch,
            expected_head=expected_head,
            extra_commit_paths=extra_paths,
        )
    except landing.LandingError as e:
        if journal_opted_in:
            truncate_journal(journal_path, snapshot_len)
        return _fail(EXIT_PYTEST_FAILURE, str(e), aborted=True)

    if sha == landing.HEAD_MOVED:
        if journal_opted_in:
            truncate_journal(journal_path, snapshot_len)
        return _fail(EXIT_PYTEST_FAILURE, "head moved; re-run jaunt jobs land")
    if sha is None:
        if journal_opted_in:
            truncate_journal(journal_path, snapshot_len)
        jobs_mod.mark(root, job, jobs_mod.SUPERSEDED)
        return _fail(
            EXIT_PYTEST_FAILURE,
            "conflict applying proposal; superseded -- the daemon will rebuild",
            state=jobs_mod.SUPERSEDED,
        )

    jobs_mod.mark(root, job, jobs_mod.LANDED, landed_commit=sha, phase="")
    return _landed(sha)


def _land_all_proposals(root: Path, args: argparse.Namespace, *, json_mode: bool = False) -> int:
    from jaunt import jobs as jobs_mod

    proposals = jobs_mod.list_jobs(root, states={jobs_mod.PROPOSED})
    if not proposals:
        if json_mode:
            _emit_json(
                {"command": "jobs", "action": "land", "ok": True, "landed": [], "results": []}
            )
        return EXIT_OK

    all_ok = True
    results: list[dict[str, object]] = []
    for job in proposals:
        code, aborted, payload = _land_one_proposal(root, args, job, json_mode=json_mode)
        results.append(payload)
        if code != EXIT_OK:
            all_ok = False
        if aborted:
            break

    if json_mode:
        _emit_json(
            {
                "command": "jobs",
                "action": "land",
                "ok": all_ok,
                "landed": [p["sha"] for p in results if p["ok"]],
                "results": results,
            }
        )
    return EXIT_OK if all_ok else EXIT_PYTEST_FAILURE


def _emit_jobs_action_error(action: str, job_id: str | None, state: str | None, msg: str) -> None:
    _emit_json(
        {
            "command": "jobs",
            "action": action,
            "ok": False,
            "job_id": job_id,
            "state": state,
            "sha": None,
            "error": msg,
        }
    )


def _cmd_jobs_land(args: argparse.Namespace) -> int:
    from jaunt import jobs as jobs_mod

    json_mode = _is_json_mode(args)
    root = Path(args.root).resolve()
    if args.all:
        return _land_all_proposals(root, args, json_mode=json_mode)
    if args.job_id is None:
        msg = "error: jobs land requires a job id or --all"
        if json_mode:
            _emit_jobs_action_error("land", None, None, msg)
        else:
            _eprint(msg)
        return EXIT_CONFIG_OR_DISCOVERY

    job = jobs_mod.load_job(root, args.job_id)
    if job is None:
        msg = f"error: job not found: {args.job_id}"
        if json_mode:
            _emit_jobs_action_error("land", args.job_id, None, msg)
        else:
            _eprint(msg)
        return EXIT_CONFIG_OR_DISCOVERY
    if job.state != jobs_mod.PROPOSED:
        msg = f"error: job {job.id} is {job.state}; only proposed jobs can be landed"
        if json_mode:
            _emit_jobs_action_error("land", job.id, job.state, msg)
        else:
            _eprint(msg)
        return EXIT_CONFIG_OR_DISCOVERY

    code, _aborted, payload = _land_one_proposal(root, args, job, json_mode=json_mode)
    if json_mode:
        _emit_json({"command": "jobs", "action": "land", **payload})
    return code


def _cmd_jobs_discard(args: argparse.Namespace) -> int:
    from jaunt import jobs as jobs_mod
    from jaunt import journal as journal_mod

    json_mode = _is_json_mode(args)
    root = Path(args.root).resolve()
    job = jobs_mod.load_job(root, args.job_id)
    if job is None:
        msg = f"error: job not found: {args.job_id}"
        if json_mode:
            _emit_jobs_action_error("discard", args.job_id, None, msg)
        else:
            _eprint(msg)
        return EXIT_CONFIG_OR_DISCOVERY
    if job.state != jobs_mod.PROPOSED:
        msg = f"error: job {job.id} is {job.state}; only proposed jobs can be discarded"
        if json_mode:
            _emit_jobs_action_error("discard", job.id, job.state, msg)
        else:
            _eprint(msg)
        return EXIT_CONFIG_OR_DISCOVERY

    jobs_mod.mark(root, job, jobs_mod.DISCARDED)
    (jobs_mod.jobs_dir(root) / f"{job.id}.patch").unlink(missing_ok=True)
    journal_mod.append_events(
        root,
        [journal_mod.JournalEvent("job-discard", _job_artifact_label(job), "discarded", job.id)],
    )
    if json_mode:
        _emit_json(
            {
                "command": "jobs",
                "action": "discard",
                "ok": True,
                "job_id": job.id,
                "state": jobs_mod.DISCARDED,
                "sha": None,
                "error": None,
            }
        )
    else:
        print(f"discarded {job.id}")
    return EXIT_OK


def cmd_jobs(args: argparse.Namespace) -> int:
    if args.jobs_command == "show":
        return _cmd_jobs_show(args)
    if args.jobs_command == "retry":
        return _cmd_jobs_retry(args)
    if args.jobs_command == "land":
        return _cmd_jobs_land(args)
    if args.jobs_command == "discard":
        return _cmd_jobs_discard(args)
    if args.jobs_command == "wait":
        return _cmd_jobs_wait(args)
    return _cmd_jobs_list(args)


def _sync_generated_dir_env(cfg: JauntConfig) -> None:
    """Propagate generated_dir to env so runtime forwarding uses the right path."""
    os.environ["JAUNT_GENERATED_DIR"] = cfg.paths.generated_dir


def _maybe_load_dotenv(root: Path) -> None:
    # Best-effort; never override existing environment variables.
    load_dotenv_into_environ(root / ".env")


def cmd_init(args: argparse.Namespace) -> int:
    from jaunt import journal as _journal

    json_mode = _is_json_mode(args)
    root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    toml_path = root / "jaunt.toml"

    if toml_path.exists() and not getattr(args, "force", False):
        msg = f"jaunt.toml already exists at {toml_path}. Use --force to overwrite."
        _eprint(f"error: {msg}")
        if json_mode:
            _emit_json({"command": "init", "ok": False, "error": msg})
        return EXIT_CONFIG_OR_DISCOVERY

    if getattr(args, "language", "py") == "ts":
        return _cmd_init_typescript(root=root, toml_path=toml_path, json_mode=json_mode)

    # Ensure default directories exist.
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)

    toml_path.write_text(INIT_TEMPLATE, encoding="utf-8")
    spec_path = root / "src" / "specs.py"
    spec_created = False
    if not spec_path.exists():
        spec_path.write_text(INIT_SPEC_TEMPLATE, encoding="utf-8")
        spec_created = True

    (root / _journal.JOURNAL_FILE).touch(exist_ok=True)
    _journal.ensure_union_merge_attribute(root)
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".jaunt/" not in existing.splitlines():
        joiner = "" if (not existing or existing.endswith("\n")) else "\n"
        gitignore.write_text(existing + joiner + ".jaunt/\n", encoding="utf-8")

    if json_mode:
        payload = {"command": "init", "ok": True, "path": str(toml_path)}
        if spec_created:
            payload["spec_path"] = str(spec_path)
        _emit_json(payload)

    return EXIT_OK


def _cmd_init_typescript(*, root: Path, toml_path: Path, json_mode: bool) -> int:
    """Scaffold a static-first TypeScript project without installing packages."""

    from jaunt import journal as _journal

    package_path = root / "package.json"
    package_mode: Literal["esm", "commonjs"] = "esm"
    package_init_command: str | None
    if not package_path.exists():
        package_init_command = "npm init -y && npm pkg set type=module"
    else:
        try:
            package_manifest = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            msg = f"Cannot scaffold TypeScript from invalid {package_path}: {error}"
            _eprint(f"error: {msg}")
            if json_mode:
                _emit_json({"command": "init", "ok": False, "error": msg})
            return EXIT_CONFIG_OR_DISCOVERY
        if not isinstance(package_manifest, dict):
            msg = f"Cannot scaffold TypeScript: {package_path} must contain a JSON object."
            _eprint(f"error: {msg}")
            if json_mode:
                _emit_json({"command": "init", "ok": False, "error": msg})
            return EXIT_CONFIG_OR_DISCOVERY

        package_type = package_manifest.get("type")
        if package_type == "commonjs":
            package_mode = "commonjs"
            package_init_command = None
        elif package_type == "module":
            package_init_command = None
        elif package_type is None:
            # Keep package.json user-owned and tell the caller how to opt the
            # existing package into the default ESM scaffold.
            package_init_command = "npm pkg set type=module"
        else:
            msg = (
                "Cannot scaffold TypeScript: package.json type must be "
                f'"module" or "commonjs", not {package_type!r}.'
            )
            _eprint(f"error: {msg}")
            if json_mode:
                _emit_json({"command": "init", "ok": False, "error": msg})
            return EXIT_CONFIG_OR_DISCOVERY

    root.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    def write_new(path: Path, content: str) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)

    toml_path.write_text(TYPESCRIPT_INIT_TEMPLATE, encoding="utf-8")
    created.append(toml_path)
    write_new(root / "src" / "index.jaunt.ts", TYPESCRIPT_SPEC_TEMPLATE)
    write_new(root / "src" / "index.context.ts", TYPESCRIPT_CONTEXT_TEMPLATE)
    write_new(root / "src" / "index.ts", TYPESCRIPT_FACADE_TEMPLATE)
    write_new(root / "tests" / "index.jaunt-test.ts", TYPESCRIPT_TEST_SPEC_TEMPLATE)
    tsconfig_template = (
        TYPESCRIPT_COMMONJS_TSCONFIG_TEMPLATE
        if package_mode == "commonjs"
        else TYPESCRIPT_TSCONFIG_TEMPLATE
    )
    write_new(root / "tsconfig.json", tsconfig_template)
    write_new(root / "tsconfig.test.json", TYPESCRIPT_TEST_TSCONFIG_TEMPLATE)

    (root / _journal.JOURNAL_FILE).touch(exist_ok=True)
    _journal.ensure_union_merge_attribute(root)
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".jaunt/" not in existing.splitlines():
        joiner = "" if (not existing or existing.endswith("\n")) else "\n"
        gitignore.write_text(existing + joiner + ".jaunt/\n", encoding="utf-8")

    if (root / "pnpm-lock.yaml").exists():
        install_command = (
            "pnpm add -D @usejaunt/ts@next 'typescript@^5.9' vitest fast-check @types/node"
        )
    else:
        install_command = (
            "npm install -D @usejaunt/ts@next 'typescript@^5.9' vitest fast-check @types/node"
        )
    if json_mode:
        _emit_json(
            {
                "command": "init",
                "ok": True,
                "language": "ts",
                "path": str(toml_path),
                "created": [str(path) for path in created],
                "package_init_command": package_init_command,
                "install_command": install_command,
            }
        )
    else:
        print(f"Initialized TypeScript Jaunt project at {root}.")
        if package_init_command:
            print(f"Configure the npm package manifest:\n  {package_init_command}")
        print(f"Install the project-local analyzer and compiler:\n  {install_command}")
        print("Then run `jaunt sync` before the first model-backed build.")
    return EXIT_OK


def _find_generated_dirs(roots: Sequence[Path], generated_dir: str) -> list[Path]:
    """Walk configured roots and find generated directories."""
    found: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, _filenames in os.walk(root):
            if Path(dirpath).name == generated_dir:
                found.add(Path(dirpath))
                dirnames.clear()  # Don't recurse into the generated dir itself
    return sorted(found)


def _find_project_orphans(
    *,
    root: Path,
    cfg: JauntConfig,
    source_dirs: Sequence[Path],
    test_dirs: Sequence[Path],
    governed_modules: set[str],
    governed_modules_by_owner: dict[Path, set[str]] | None = None,
    contract_refs: set[str] | None,
    include_artifacts: bool = True,
    classify_test_orphans: bool = True,
):
    """Collect orphaned jaunt artifacts across (possibly nested) generated dirs.

    - `governed_modules` is the workspace-wide union used for side-by-side stubs.
      Generated modules use `governed_modules_by_owner` when supplied so the same
      test-module name in another package cannot keep an orphan alive.
    - Generated dirs are scanned under BOTH the source roots and the test roots
      (deduplicated): the tester writes generated tests under the test roots'
      `__generated__`, which is invisible when a project's source roots do not
      nest them.
    - `include_artifacts=False` skips generated/stub/sidecar scanning (used when
      the magic side of a check is out of scope).
    - `contract_refs=None` skips contract-battery scanning (e.g. --magic-only, or
      when the contract side of a check is out of scope).
    - `classify_test_orphans=False` never flags generated TEST modules (fail-safe
      when the governed test-module set is incomplete).
    """
    from jaunt.reconcile import OrphanArtifact, find_orphans
    from jaunt.workspace import nearest_pyproject

    seen: dict[Path, OrphanArtifact] = {}
    existing_source = [d for d in source_dirs if d.exists()]
    existing_test = [d for d in test_dirs if d.exists()]

    if include_artifacts:
        # Generated modules + their sidecars live under each generated dir; the
        # generated dir's PARENT is the package_dir find_orphans scans. Scan under
        # source AND test roots (deduplicated by _find_generated_dirs).
        scan_dirs = [*existing_source, *existing_test]
        for gen_root in _find_generated_dirs(scan_dirs, cfg.paths.generated_dir):
            owner_pyproject = nearest_pyproject(gen_root, config_root=root)
            owner = owner_pyproject.parent if owner_pyproject is not None else root.resolve()
            owner_governed = (
                governed_modules
                if governed_modules_by_owner is None
                else governed_modules_by_owner.get(owner, set())
            )
            for o in find_orphans(
                package_dir=gen_root.parent,
                generated_dir=cfg.paths.generated_dir,
                governed_modules=owner_governed,
                source_dirs=[],
                battery_dir=None,
                contract_refs=set(),
                classify_test_orphans=classify_test_orphans,
            ):
                seen.setdefault(o.path, o)
        # Stubs live next to spec sources under the source roots.
        if existing_source:
            for o in find_orphans(
                package_dir=existing_source[0],
                generated_dir="__jaunt_no_such_generated__",
                governed_modules=governed_modules,
                source_dirs=existing_source,
                battery_dir=None,
                contract_refs=set(),
                classify_test_orphans=classify_test_orphans,
            ):
                seen.setdefault(o.path, o)

    if contract_refs is not None:
        from jaunt.workspace import resolve_workspace

        workspace = resolve_workspace(root, cfg)
        owners = workspace.owner_dirs or (root.resolve(),)
        for owner in owners:
            battery_dir = owner / cfg.contract.battery_dir
            if not battery_dir.exists():
                continue
            for o in find_orphans(
                package_dir=owner,
                generated_dir="__jaunt_no_such_generated__",
                governed_modules=governed_modules,
                source_dirs=[],
                battery_dir=battery_dir,
                contract_refs=contract_refs,
            ):
                seen.setdefault(o.path, o)

    return sorted(seen.values(), key=lambda o: str(o.path))


def _discover_governed_test_modules(
    root: Path, cfg: JauntConfig
) -> tuple[dict[Path, set[str]], bool]:
    """Governed test-module names by owner and orphan-classification safety.

    Each returned owner set holds the module names that appear as
    `source_module` in generated test headers:

    - Explicit `@jaunt.test` specs, read from the test registry after importing
      the marker-discovered test modules (keyed exactly as the tester keys
      generated-test headers). A per-module import failure fails safe by treating
      only THAT module as governed — marker PRESENCE alone is never governance,
      so a module that kept `import jaunt` but lost its last `@jaunt.test` spec
      correctly orphans its stale generated tests.
    - Synthesized auto-class-test module names (`test=True` / `auto_class_tests`).

    The returned bool is False when the auto-class synthesis pass raised: the
    governed set is then incomplete, so callers must NOT classify generated-test
    orphans this run (never delete against a partial set).
    """
    import importlib

    from jaunt import discovery, registry

    governed: dict[Path, set[str]] = {}

    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    _prepend_sys_path([root, *workspace.source_roots])
    for owner in workspace.owner_dirs:
        owner_governed = governed.setdefault(owner, set())
        _prepend_sys_path([owner])
        owner_routes = workspace.tests_for_owner(owner)
        test_dirs = [route.root for route in owner_routes]
        test_modules: set[str] = set()
        for route in owner_routes:
            test_modules.update(
                discovery.discover_modules(
                    roots=[route.root],
                    exclude=[],
                    generated_dir=cfg.paths.generated_dir,
                    module_prefix=route.module_prefix,
                )
            )
        if not test_modules:
            continue
        discovery.prepare_import_environment(module_names=sorted(test_modules), roots=test_dirs)
        for module in sorted(test_modules):
            try:
                importlib.import_module(module)
            except Exception:  # noqa: BLE001 - per-module fail-safe: keep its tests non-orphan
                owner_governed.add(module)
        owner_governed.update(registry.get_specs_by_module("test").keys())

    # Auto-class test modules require magic specs. If the synthesis pass fails we
    # cannot know the auto module names, so disable test-orphan classification
    # entirely rather than judge against a partial set.
    source_dirs = list(workspace.source_roots)
    if not source_dirs:
        return governed, True
    try:
        from jaunt.module_contract import synthesize_auto_class_test_entries

        _prepend_sys_path([*source_dirs, root])
        mods = [route.module for route in workspace.modules]
        discovery.prepare_import_environment(module_names=mods, roots=source_dirs)
        discovery.import_and_collect(mods, kind="magic")
        magic_specs = registry.get_magic_registry()
        for owner in workspace.owner_dirs:
            owner_modules = {
                route.module for route in workspace.modules if route.owner_dir == owner
            }
            owner_specs = {
                ref: entry for ref, entry in magic_specs.items() if entry.module in owner_modules
            }
            tests_package = workspace.primary_test_root(owner).module_prefix
            auto = synthesize_auto_class_test_entries(
                owner_specs,
                default_on=bool(cfg.test.auto_class_tests),
                tests_package=tests_package,
                generated_dir=cfg.paths.generated_dir,
            )
            governed.setdefault(owner, set()).update(auto.keys())
    except Exception as exc:  # noqa: BLE001 - fail safe: skip test-orphan detection this run
        _eprint(
            f"warning: could not enumerate auto-class test modules "
            f"({type(exc).__name__}: {exc}); skipping generated-test orphan detection this run"
        )
        return governed, False
    return governed, True


def _governed_modules_by_owner(
    workspace: ResolvedWorkspace,
    magic_modules: set[str],
    test_modules: dict[Path, set[str]],
) -> dict[Path, set[str]]:
    """Combine magic and test governance without crossing package boundaries."""

    combined = {owner: set(modules) for owner, modules in test_modules.items()}
    for module in magic_modules:
        combined.setdefault(workspace.route_for(module).owner_dir, set()).add(module)
    return combined


def _discover_reconcile_sets(root: Path, cfg: JauntConfig) -> tuple[set[str], set[str]]:
    """Discover currently-governed magic module names and contract refs."""
    from jaunt import discovery, registry
    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    existing = list(workspace.source_roots)
    _prepend_sys_path([*existing, root])
    mods = [route.module for route in workspace.modules]
    discovery.prepare_import_environment(module_names=mods, roots=existing)
    discovery.import_and_collect(mods, kind="magic")
    governed = set(registry.get_specs_by_module("magic").keys())
    contract_specs = _discover_contract_specs(root=root, cfg=cfg)
    contract_refs = {str(e.spec_ref) for e in contract_specs.values()}
    return governed, contract_refs


def _find_jaunt_stubs(roots: Sequence[Path], generated_dir: str) -> list[Path]:
    from jaunt import stub_emitter

    found: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name != generated_dir]
            for filename in filenames:
                if not filename.endswith(".pyi"):
                    continue
                path = Path(dirpath) / filename
                if stub_emitter.is_jaunt_stub(path):
                    found.add(path)
    return sorted(found)


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def cmd_tree(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        from jaunt.repo_context import api as rc_api

        if getattr(args, "check", False):
            drift = rc_api.check_drift(root=root, cfg=cfg)
            if json_mode:
                _emit_json(
                    {
                        "command": "tree",
                        "ok": drift is None,
                        "drift": None
                        if drift is None
                        else {
                            "added": drift.added,
                            "removed": drift.removed,
                            "restaled": drift.restaled,
                        },
                    }
                )
            elif drift is None:
                print("treedocs.yaml is up to date.")
            else:
                _eprint(
                    f"drift: +{len(drift.added)} new, -{len(drift.removed)} removed, "
                    f"~{len(drift.restaled)} stale description(s). Run `jaunt tree`."
                )
            return EXIT_OK if drift is None else 4

        doc, result = rc_api.sync_tree(root=root, cfg=cfg, today=_today())
        if json_mode:
            _emit_json(
                {
                    "command": "tree",
                    "ok": True,
                    "added": result.added,
                    "removed": result.removed,
                    "restaled": result.restaled,
                }
            )
        else:
            print(
                f"Synced {cfg.context.repo_map_file}: "
                f"+{len(result.added)} new, -{len(result.removed)} removed, "
                f"~{len(result.restaled)} updated."
            )
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "tree", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY


def cmd_clean(args: argparse.Namespace) -> int:
    import shutil

    context = _typescript_command_context(args)
    if context is not None:
        root, cfg, mode = context
        if (failure := _target_dispatch_failure(args, mode)) is not None:
            return failure
        if mode == "ts":
            return _cmd_typescript_clean_loaded(args, root, cfg)
        if mode == "mixed":
            return _cmd_mixed_clean(args, root, cfg)

    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
    except (JauntConfigError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "clean", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    if getattr(args, "orphans", False):
        governed_modules, contract_refs = _discover_reconcile_sets(root, cfg)
        test_governed, classify_test_orphans = _discover_governed_test_modules(root, cfg)
        from jaunt.workspace import resolve_workspace

        workspace = resolve_workspace(root, cfg)
        governed_by_owner = _governed_modules_by_owner(workspace, governed_modules, test_governed)
        governed_modules = set().union(*governed_by_owner.values())
        source_dirs = list(workspace.source_roots)
        test_dirs = list(workspace.artifact_test_roots())
        orphans = _find_project_orphans(
            root=root,
            cfg=cfg,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            governed_modules=governed_modules,
            governed_modules_by_owner=governed_by_owner,
            contract_refs=contract_refs,
            classify_test_orphans=classify_test_orphans,
        )
        orphan_rels = [str(o.path.relative_to(root)) for o in orphans]
        if getattr(args, "dry_run", False):
            if json_mode:
                _emit_json(
                    {
                        "command": "clean",
                        "ok": True,
                        "dry_run": True,
                        "orphans": True,
                        "would_remove": orphan_rels,
                    }
                )
            return EXIT_OK

        for orphan in orphans:
            orphan.path.unlink(missing_ok=True)

        from jaunt import journal

        journal.append_events(
            root,
            [
                journal.JournalEvent(
                    action="orphan-removed",
                    module=o.source_module,
                    detail=str(o.path.relative_to(root)),
                )
                for o in orphans
            ],
        )
        if json_mode:
            _emit_json(
                {
                    "command": "clean",
                    "ok": True,
                    "orphans": True,
                    "removed": orphan_rels,
                }
            )
        return EXIT_OK

    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    generated_dir = cfg.paths.generated_dir
    scan_roots = [*workspace.source_roots, *workspace.artifact_test_roots()]
    scan_roots.extend(owner / cfg.contract.battery_dir for owner in workspace.owner_dirs)
    found = _find_generated_dirs(scan_roots, generated_dir)
    stubs = _find_jaunt_stubs(scan_roots, generated_dir)
    dry_run = getattr(args, "dry_run", False)

    if dry_run:
        if json_mode:
            _emit_json(
                {
                    "command": "clean",
                    "ok": True,
                    "dry_run": True,
                    "would_remove": [str(p) for p in [*found, *stubs]],
                }
            )
        return EXIT_OK

    for d in found:
        shutil.rmtree(d)
    for stub in stubs:
        try:
            stub.unlink()
        except FileNotFoundError:
            pass

    if json_mode:
        _emit_json(
            {
                "command": "clean",
                "ok": True,
                "removed": [str(p) for p in [*found, *stubs]],
            }
        )

    return EXIT_OK


@dataclass(frozen=True, slots=True)
class _BuildDiscoveryContext:
    package_dir: Path
    workspace: "ResolvedWorkspace"
    specs: dict["SpecRef", "SpecEntry"]
    spec_graph: dict["SpecRef", set["SpecRef"]]
    module_dag: dict[str, set[str]]
    module_specs: dict[str, list["SpecEntry"]]
    header_fields_by_module: dict[str, dict[str, object]]


def _newly_governed_for_workspace(
    entries: Sequence["SpecEntry"],
    *,
    workspace: "ResolvedWorkspace",
    generated_dir: str,
) -> dict[str, list[str]]:
    from jaunt import builder

    grouped: dict[str, list[str]] = {}
    present: dict[str, bool] = {}
    for entry in entries:
        if entry.origin != "module":
            continue
        exists = present.get(entry.module)
        if exists is None:
            route = workspace.route_for(entry.module)
            exists = (
                builder._read_generated(
                    route.output_base,
                    generated_dir,
                    entry.module,
                )
                is not None
            )
            present[entry.module] = exists
        if not exists:
            grouped.setdefault(entry.module, []).append(entry.qualname)
    return {module: sorted(symbols) for module, symbols in grouped.items()}


def _discover_build_context(
    root: Path, cfg: JauntConfig, args: argparse.Namespace
) -> _BuildDiscoveryContext:
    _maybe_load_dotenv(root)
    _sync_generated_dir_env(cfg)
    include_target_tests = _effective_include_target_tests(cfg, args)
    build_instructions = _effective_build_instructions(cfg, args)
    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    source_dirs = list(workspace.source_roots)
    package_dir = source_dirs[0]

    _prepend_sys_path([*source_dirs, root])

    from jaunt import builder, discovery, registry
    from jaunt.deps import build_spec_graph, collapse_to_module_dag, find_cycles
    from jaunt.digest import legacy_module_digest
    from jaunt.generation_fingerprint import generation_fingerprint
    from jaunt.module_api import module_api_digest
    from jaunt.module_contract import group_test_entries_by_target_module

    modules = [route.module for route in workspace.modules]
    discovery.prepare_import_environment(
        module_names=modules,
        roots=[d for d in source_dirs if d.exists()],
    )
    discovery.import_and_collect(modules, kind="magic")
    static_targeted_test_entries = (
        _discover_static_targeted_test_entries(root=root, cfg=cfg) if include_target_tests else []
    )

    specs = dict(registry.get_magic_registry())
    infer_default = bool(cfg.build.infer_deps) and not bool(getattr(args, "no_infer_deps", False))
    spec_graph = build_spec_graph(specs, infer_default=infer_default)
    cycles = find_cycles(spec_graph)
    if cycles:
        raise JauntDependencyCycleError(
            "Dependency cycle detected: "
            + ", ".join(" -> ".join(str(s) for s in c) for c in cycles)
        )
    module_dag = collapse_to_module_dag(spec_graph)
    module_specs = registry.get_specs_by_module("magic")
    build_generation_fingerprint = generation_fingerprint(
        cfg,
        kind="build",
        build_instructions=build_instructions,
        include_target_tests=include_target_tests,
    )
    targeted_test_entries = group_test_entries_by_target_module(static_targeted_test_entries)

    try:
        module_digest_fn = builder.module_digest
    except AttributeError:
        from jaunt.digest import module_digest as module_digest_fn

    header_fields_by_module: dict[str, dict[str, object]] = {}
    for module_name, entries in sorted(module_specs.items()):
        module_dir = workspace.route_for(module_name).output_base
        expected, _errs = builder._build_expected_names(entries)
        wcc = builder._whole_class_context(
            entries,
            specs=specs,
            package_dir=module_dir,
            generated_dir=cfg.paths.generated_dir,
            module_output_bases=workspace.output_bases,
        )
        module_context = builder.build_module_context_artifacts(
            module_name=module_name,
            entries=entries,
            expected_names=expected,
            module_specs=module_specs,
            module_dag=module_dag,
            package_dir=module_dir,
            generated_dir=cfg.paths.generated_dir,
            build_instructions=build_instructions,
            targeted_test_entries=targeted_test_entries,
            base_contract_block=wcc.base_contract_block,
            whole_class_contract_block=wcc.whole_class_contract_block,
            inherited_api_block=wcc.inherited_api_block,
        )
        fields: dict[str, object] = {
            "tool_version": builder._tool_version(),
            "kind": "build",
            "source_module": module_name,
            "module_digest": module_digest_fn(module_name, entries, specs, spec_graph),
            "legacy_module_digest": legacy_module_digest(module_name, entries, specs, spec_graph),
            "generation_fingerprint": build_generation_fingerprint,
            "module_context_digest": module_context.digest,
            "module_api_digest": module_api_digest(entries),
            "spec_refs": [str(e.spec_ref) for e in entries],
        }
        if wcc.base_api_digest:
            fields["base_api_digest"] = wcc.base_api_digest
        header_fields_by_module[module_name] = fields

    return _BuildDiscoveryContext(
        package_dir=package_dir,
        workspace=workspace,
        specs=specs,
        spec_graph=spec_graph,
        module_dag=module_dag,
        module_specs=module_specs,
        header_fields_by_module=header_fields_by_module,
    )


def _action_json(action, root: Path) -> dict[str, str]:
    try:
        path = str(action.path.resolve().relative_to(root.resolve()))
    except ValueError:
        path = str(action.path)
    return {
        "migration": action.migration_id,
        "path": path,
        "module": action.module,
        "symbol": action.symbol,
        "kind": action.kind,
        "classification": action.classification,
        "description": action.description,
    }


def _is_dirty_worktree(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def _reemit_stub_for_module(
    *,
    module_name: str,
    entries: list["SpecEntry"],
    package_dir: Path,
    generated_dir: str,
    tool_version: str,
) -> str | None:
    from jaunt import builder, header, paths, stub_emitter

    gen_source = builder._read_generated(package_dir, generated_dir, module_name)
    if gen_source is None:
        return None
    expected, _ = builder._build_expected_names(entries)
    spec_source = Path(entries[0].source_file).read_text(encoding="utf-8")
    stub_path = stub_emitter.stub_path_for_source(entries[0].source_file)
    if stub_path.exists() and not stub_emitter.is_jaunt_stub(stub_path):
        return None
    stub_header = header.format_stub_header(
        tool_version=tool_version,
        source_module=module_name,
        generated_digest=stub_emitter.generated_content_digest(gen_source),
        inputs_digest=stub_emitter.stub_inputs_digest(spec_source, gen_source),
    )
    new_stub = stub_emitter.format_stub_best_effort(
        stub_emitter.build_stub_source(
            spec_source,
            gen_source,
            set(expected),
            stub_header,
            generated_module=paths.spec_module_to_generated_module(
                module_name, generated_dir=generated_dir
            ),
        )
    )
    if stub_path.exists() and stub_path.read_text(encoding="utf-8") == new_stub:
        return str(stub_path)
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(stub_path.parent),
        prefix=".jaunt-stub-tmp-",
        suffix=".pyi",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_stub)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, stub_path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return str(stub_path)


def _migrate_candidate_files(root: Path, cfg: JauntConfig) -> dict[Path, str]:
    """Spec-marker source files eligible for legacy-stub rewrites.

    Every `.py` under the configured source roots that carries a jaunt marker,
    excluding generated dirs, hidden dirs, and files under the test roots.
    Returns a mapping of resolved path -> discovered module name, deduplicated by
    path (a file reachable under overlapping roots is planned once).

    Test roots and top-level hidden dirs (`.venv`, `.git`, …) are pruned from the
    walk itself via `exclude` globs so a large test tree is never read; the
    post-filter still guards nested hidden dirs and non-root test trees.
    """
    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    return {
        route.source_file: route.module
        for route in workspace.modules
        if not any(part.startswith(".") for part in route.source_file.relative_to(root).parts)
    }


def _cmd_migrate_config_v2(args: argparse.Namespace, *, json_mode: bool) -> int:
    """Plan or atomically apply the Python-neutral config-v2 rewrite."""

    try:
        root, _cfg = _load_config(args)
        from jaunt.typescript.migrate import apply_config_v2, plan_config_v2

        config_path = Path(args.config).resolve() if args.config else root / "jaunt.toml"
        plan = plan_config_v2(root, config_path)
        details = plan.to_json(root)
        if not bool(getattr(args, "apply", False)):
            if json_mode:
                _emit_json(
                    {
                        "command": "migrate",
                        "ok": True,
                        "applied": False,
                        **details,
                        **({"content": plan.source} if plan.changed else {}),
                    }
                )
            elif not plan.changed:
                print("jaunt.toml already uses config version 2.")
            else:
                print(f"Would migrate {details['path']} to config version 2:")
                print(plan.source, end="")
            return EXIT_OK

        if plan.changed and not bool(getattr(args, "force", False)) and _is_dirty_worktree(root):
            error = "dirty working tree"
            if json_mode:
                _emit_json(
                    {
                        "command": "migrate",
                        "ok": False,
                        "applied": False,
                        "error": error,
                        **details,
                    }
                )
            else:
                _eprint(
                    "error: refusing to migrate config; git working tree is dirty (use --force)"
                )
            return EXIT_CONFIG_OR_DISCOVERY
        applied = apply_config_v2(plan)
        if json_mode:
            _emit_json(
                {
                    "command": "migrate",
                    "ok": True,
                    "applied": applied,
                    **details,
                }
            )
        elif applied:
            print("Migrated jaunt.toml to config version 2 without changing Python routes.")
        else:
            print("jaunt.toml already uses config version 2.")
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, KeyError) as exc:
        _print_error(exc)
        if json_mode:
            _emit_json({"command": "migrate", "ok": False, "error": str(exc)})
        return EXIT_CONFIG_OR_DISCOVERY


def _cmd_typescript_migrate_loaded(args: argparse.Namespace, root: Path, cfg: JauntConfig) -> int:
    """Plan or atomically apply worker-validated TypeScript artifact repairs."""

    from jaunt.typescript.migrate import apply_typescript_migration, plan_typescript_migration

    json_mode = _is_json_mode(args)
    try:
        plan = asyncio.run(plan_typescript_migration(root, cfg))
        payload = plan.to_json()
        if not bool(getattr(args, "apply", False)):
            if json_mode:
                _emit_json(payload)
            elif not plan.actions:
                print("No pending TypeScript migrations.")
            else:
                print("TypeScript migration plan (no files changed):")
                for action in plan.actions:
                    print(f"- {action.description}")
            return EXIT_OK

        if plan.blocked:
            error = "TypeScript migration requires manual intervention; no artifacts were written"
            if json_mode:
                _emit_json({**payload, "ok": False, "applied": False, "error": error})
            else:
                _eprint(f"error: {error}")
                for diagnostic in plan.diagnostics:
                    if diagnostic.classification == "manual-intervention":
                        _eprint(f"- {diagnostic.code}: {diagnostic.message}")
            return EXIT_CONFIG_OR_DISCOVERY
        if plan.writes and not bool(getattr(args, "force", False)) and _is_dirty_worktree(root):
            error = "dirty working tree"
            if json_mode:
                _emit_json({**payload, "ok": False, "applied": False, "error": error})
            else:
                _eprint(
                    "error: refusing to migrate TypeScript artifacts; "
                    "git working tree is dirty (use --force)"
                )
            return EXIT_CONFIG_OR_DISCOVERY

        applied_paths = apply_typescript_migration(plan)
        applied_payload = plan.to_json(applied=True, applied_paths=applied_paths)
        if json_mode:
            _emit_json(applied_payload)
        else:
            if applied_paths:
                print(f"Applied {len(applied_paths)} TypeScript artifact migration(s):")
                for path in applied_paths:
                    print(f"- {path}")
            else:
                print("No pending TypeScript migrations.")
            for module_id in plan.requires_rebuild:
                print(f"Needs model rebuild: {module_id}")
        return EXIT_OK
    except JauntGenerationError as error:
        return _typescript_error("migrate", error, json_mode=json_mode, code=EXIT_GENERATION_ERROR)
    except (JauntConfigError, JauntDiscoveryError, KeyError) as error:
        return _typescript_error(
            "migrate", error, json_mode=json_mode, code=EXIT_CONFIG_OR_DISCOVERY
        )


def cmd_migrate(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    if bool(getattr(args, "config_v2", False)):
        if bool(getattr(args, "merge_projects", False)):
            error = "--config-v2 and --merge-projects are separate migrations"
            if json_mode:
                _emit_json({"command": "migrate", "ok": False, "error": error})
            else:
                _eprint(f"error: {error}")
            return EXIT_CONFIG_OR_DISCOVERY
        return _cmd_migrate_config_v2(args, json_mode=json_mode)
    if bool(getattr(args, "merge_projects", False)):
        try:
            root, _cfg = _load_config(args)
            from jaunt.workspace_merge import apply_merge, plan_merge

            plan = plan_merge(root)
            payload = plan.to_json(root)
            if not bool(getattr(args, "apply", False)):
                if json_mode:
                    _emit_json({"command": "migrate", "ok": plan.neutral, **payload})
                else:
                    print("Merge-projects plan:")
                    for action in plan.actions:
                        print(f"- {action['action']}: {action['path']}")
                    for conflict in plan.conflicts:
                        print(f"[CONFLICT] {conflict}")
                return EXIT_OK if plan.neutral else EXIT_CONFIG_OR_DISCOVERY
            if not plan.neutral:
                if json_mode:
                    _emit_json({"command": "migrate", "ok": False, **payload})
                else:
                    for conflict in plan.conflicts:
                        _eprint(f"error: {conflict}")
                return EXIT_CONFIG_OR_DISCOVERY
            if not bool(getattr(args, "force", False)) and _is_dirty_worktree(root):
                error = "dirty working tree"
                if json_mode:
                    _emit_json({"command": "migrate", "ok": False, "error": error, **payload})
                else:
                    _eprint("error: refusing to merge projects; git working tree is dirty")
                return EXIT_CONFIG_OR_DISCOVERY
            applied, error = apply_merge(root, plan)
            if json_mode:
                _emit_json(
                    {
                        "command": "migrate",
                        "ok": applied,
                        "applied": applied,
                        **payload,
                        **({"error": error} if error else {}),
                    }
                )
            elif applied:
                print("Merged descendant Jaunt projects into the root jaunt.toml.")
            else:
                _eprint(f"error: merge rolled back: {error}")
            return EXIT_OK if applied else EXIT_CONFIG_OR_DISCOVERY
        except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError) as exc:
            _print_error(exc)
            if json_mode:
                _emit_json({"command": "migrate", "ok": False, "error": str(exc)})
            return EXIT_CONFIG_OR_DISCOVERY

    try:
        root, cfg = _load_config(args)
        mode = _target_dispatch_mode(args, cfg)
        if mode == "ts":
            return _cmd_typescript_migrate_loaded(args, root, cfg)
        ctx = _discover_build_context(root, cfg, args)
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "migrate", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    from jaunt import migrate

    # Governed qualnames per source file, keyed by resolved path. Full qualnames
    # (not collapsed to the class name) so a single governed C.method does not
    # make every legacy-bodied method in C look already-governed.
    source_governed: dict[Path, set[str]] = {}
    module_by_path: dict[Path, str] = {}
    for module, entries in ctx.module_specs.items():
        for entry in entries:
            resolved = Path(entry.source_file).resolve()
            source_governed.setdefault(resolved, set()).add(entry.qualname)
            module_by_path.setdefault(resolved, module)

    # Candidates are ALL spec-marker source files, not only files with a current
    # governed spec: a module-mode file whose every body is a legacy
    # `raise RuntimeError("spec stub")` has zero governed specs yet still needs a
    # plan (newly-governs entries).
    candidate_module_by_path = _migrate_candidate_files(root, cfg)
    for resolved, module in module_by_path.items():
        candidate_module_by_path.setdefault(resolved, module)

    legacy_actions = []
    for resolved in sorted(candidate_module_by_path, key=lambda p: str(p)):
        module = module_by_path.get(resolved, candidate_module_by_path[resolved])
        legacy_actions.extend(
            migrate.plan_legacy_stub_rewrites(
                source_file=resolved,
                module=module,
                governed_symbols=source_governed.get(resolved, set()),
            )
        )
    stub_actions = []
    for output_base in sorted(set(ctx.workspace.output_bases.values())):
        routed_specs = {
            module: entries
            for module, entries in ctx.module_specs.items()
            if ctx.workspace.route_for(module).output_base == output_base
        }
        stub_actions.extend(
            migrate.plan_stub_reemissions(
                module_specs=routed_specs,
                package_dir=output_base,
                generated_dir=cfg.paths.generated_dir,
            )
        )
    migration_order = {
        migrate.LEGACY_STUB_MIGRATION_ID: 0,
        migrate.STUB_REEMIT_MIGRATION_ID: 1,
    }
    actions = sorted(
        [*legacy_actions, *stub_actions],
        key=lambda a: (migration_order.get(a.migration_id, 99), str(a.path), a.module, a.symbol),
    )

    if not bool(getattr(args, "apply", False)):
        if json_mode:
            _emit_json(
                {
                    "command": "migrate",
                    "ok": True,
                    "applied": False,
                    "actions": [_action_json(a, root) for a in actions],
                }
            )
        elif not actions:
            print("No pending migrations.")
        else:
            print("Pending migrations:")
            for action in actions:
                print(action.description)
        return EXIT_OK

    if not bool(getattr(args, "force", False)) and _is_dirty_worktree(root):
        _eprint("error: refusing to migrate; git working tree is dirty (use --force)")
        if json_mode:
            _emit_json(
                {
                    "command": "migrate",
                    "ok": False,
                    "applied": False,
                    "error": "dirty working tree",
                }
            )
        return EXIT_CONFIG_OR_DISCOVERY

    applied_actions = []
    skipped_actions = []
    restamp_rewrite_modules: set[str] = set()
    newly_governed_rewrite_modules: set[str] = set()
    for action in actions:
        if action.migration_id == migrate.LEGACY_STUB_MIGRATION_ID:
            if action.classification == "newly-governs" and not bool(
                getattr(args, "allow_newly_governed", False)
            ):
                skipped_actions.append(action)
                if not json_mode:
                    print(
                        "SKIPPED (would newly govern; pass --allow-newly-governed): "
                        f"{action.description}"
                    )
                continue
            migrate.apply_stub_rewrite(action)
            applied_actions.append(action)
            if not json_mode:
                rel = _action_json(action, root)["path"]
                print(f"rewrote {rel}: {action.module}.{action.symbol}")
            if action.classification == "newly-governs":
                newly_governed_rewrite_modules.add(action.module)
            else:
                restamp_rewrite_modules.add(action.module)
        elif action.migration_id == migrate.STUB_REEMIT_MIGRATION_ID:
            applied_actions.append(action)

    try:
        ctx_after = _discover_build_context(root, cfg, args)
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "migrate", "ok": False, "applied": True, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    from jaunt import builder

    restamped_modules: set[str] = set()
    needs_rebuild_modules: set[str] = set()
    for module in sorted(restamp_rewrite_modules - newly_governed_rewrite_modules):
        entries = ctx_after.module_specs.get(module)
        header_fields = ctx_after.header_fields_by_module.get(module)
        if not entries or header_fields is None:
            needs_rebuild_modules.add(module)
            continue
        outcome = builder.refreeze_module(
            package_dir=ctx_after.package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_name=module,
            header_fields=builder._header_fields_with_spec_digests(header_fields, entries),
            snapshots=builder._compute_snapshots(entries),
            module_output_bases=ctx_after.workspace.output_bases,
        )
        if outcome.needs_rebuild:
            needs_rebuild_modules.add(module)
            if not json_mode:
                print(f"needs build {module}")
        else:
            restamped_modules.add(module)
            if not json_mode:
                print(f"re-stamped {module}")

    reemitted_paths: list[str] = []
    if cfg.build.emit_stubs:
        reemit_modules = restamped_modules | {
            action.module
            for action in applied_actions
            if action.migration_id == migrate.STUB_REEMIT_MIGRATION_ID
        }
        for module in sorted(reemit_modules):
            entries = ctx_after.module_specs.get(module)
            if not entries:
                continue
            stub = _reemit_stub_for_module(
                module_name=module,
                entries=entries,
                package_dir=ctx_after.workspace.route_for(module).output_base,
                generated_dir=cfg.paths.generated_dir,
                tool_version=builder._tool_version(),
            )
            if stub is None:
                continue
            reemitted_paths.append(stub)
            if not json_mode:
                try:
                    rel = str(Path(stub).resolve().relative_to(root.resolve()))
                except ValueError:
                    rel = stub
                print(f"re-emitted {rel}")

    if json_mode:
        _emit_json(
            {
                "command": "migrate",
                "ok": True,
                "applied": True,
                "actions": [_action_json(a, root) for a in applied_actions],
                "skipped": [_action_json(a, root) for a in skipped_actions],
            }
        )
    return EXIT_OK


def _fingerprint_env_hint(cfg: JauntConfig, stale_changes: dict[str, str]) -> str | None:
    """One-line diagnosis when staleness is fingerprint-only and codex is absent.

    With `[codex] fingerprint_cli_version = true`, `codex --version` is embedded
    in the generation fingerprint; an environment without the codex binary
    resolves it to "unknown" and restales a byte-identical tree. Name the cause
    instead of leaving the user to bisect fingerprint parts.
    """
    if "fingerprint" not in stale_changes.values():
        return None
    if not (cfg.agent.engine == "codex" and cfg.codex.fingerprint_cli_version):
        return None
    from jaunt.generate.fingerprint import resolve_codex_cli_version

    if resolve_codex_cli_version() != "unknown":
        return None
    return (
        "hint: stale (fingerprint) with no codex binary on PATH — "
        "[codex] fingerprint_cli_version = true embeds `codex --version` in freshness, "
        "so environments without codex restale byte-identical trees; set it to false "
        "for environment-independent checks."
    )


def _render_stale_reason(reason: str) -> str:
    """Human-facing stale label; the free re-stamp case is called out as such."""
    return "re-stamp: free" if reason == "re-stamp" else reason


def cmd_check(args: argparse.Namespace) -> int:
    context = _typescript_command_context(args)
    if context is not None:
        root, cfg, mode = context
        if (failure := _target_dispatch_failure(args, mode)) is not None:
            return failure
        if mode == "ts":
            return _cmd_typescript_check_loaded(args, root, cfg)
        if mode == "mixed":
            return _cmd_mixed_check(args, root, cfg)

    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        from jaunt.contract import runner
        from jaunt.contract.drift import BLOCKING_MESSAGE, is_blocking

        contracts_only = bool(getattr(args, "contracts_only", False))
        magic_only = bool(getattr(args, "magic_only", False))
        run_contracts = not magic_only
        run_magic = not contracts_only

        contract_results = []
        contract_blocked_results = []
        contract_checked: list[dict[str, str]] = []
        contract_blocked: list[dict[str, str]] = []
        specs = {}

        if run_contracts:
            specs = _discover_contract_specs(root=root, cfg=cfg)

            if specs:
                for entry in sorted(specs.values(), key=lambda e: str(e.spec_ref)):
                    owner, source_roots = _contract_owner_context(
                        root=root, cfg=cfg, module=entry.module
                    )

                    def _run(
                        path: Path,
                        *,
                        _owner=owner,
                        _source_roots=source_roots,
                    ) -> bool:
                        return runner.run_battery_file(
                            path,
                            root=_owner,
                            source_roots=_source_roots,
                        )

                    contract_results.append(
                        runner.evaluate_entry(
                            owner,
                            cfg.contract.battery_dir,
                            cfg.contract.derive,
                            entry,
                            run_battery=_run,
                        )
                    )
                contract_blocked_results = [r for r in contract_results if is_blocking(r.state)]
                contract_checked = [
                    {"ref": str(r.spec_ref), "state": r.state.value} for r in contract_results
                ]
                contract_blocked = [
                    {"ref": str(r.spec_ref), "state": r.state.value}
                    for r in contract_blocked_results
                ]

        magic_fresh: list[str] = []
        magic_stale: dict[str, str] = {}
        magic_unbuilt: list[str] = []
        artifact_orphan_objs: list = []
        battery_orphan_objs: list = []
        newly_governed_modules: dict[str, list[str]] = {}
        from jaunt.workspace import resolve_workspace

        workspace = resolve_workspace(root, cfg)
        source_dirs = list(workspace.source_roots)
        test_dirs = list(workspace.artifact_test_roots())
        if run_magic:
            from jaunt import builder

            _prepend_sys_path([*source_dirs, root])
            include_target_tests = _effective_include_target_tests(cfg, args)
            build_instructions = _effective_build_instructions(cfg, args)
            infer_default = bool(cfg.build.infer_deps) and (not bool(args.no_infer_deps))
            mstatus = compute_magic_status(
                root=root,
                cfg=cfg,
                source_dirs=source_dirs,
                build_instructions=build_instructions,
                include_target_tests=include_target_tests,
                infer_deps=infer_default,
                force=bool(args.force),
                target=args.target,
            )

            if mstatus.total:
                for module_name in sorted(mstatus.stale):
                    generated_missing = (
                        builder._read_generated(
                            workspace.route_for(module_name).output_base,
                            cfg.paths.generated_dir,
                            module_name,
                        )
                        is None
                    )
                    if generated_missing:
                        magic_unbuilt.append(module_name)
                    else:
                        magic_stale[module_name] = mstatus.stale_changes.get(
                            module_name, "structural"
                        )
                magic_fresh = sorted(mstatus.fresh)

            if source_dirs:
                from jaunt import registry

                governed_modules = set(registry.get_specs_by_module("magic").keys())
                entries = list(registry.get_magic_registry().values())
                newly_governed_modules = _newly_governed_for_workspace(
                    entries,
                    workspace=workspace,
                    generated_dir=cfg.paths.generated_dir,
                )
                # Judge generated artifacts (impl + tests + stubs + sidecars)
                # against the union of magic and test spec modules. Contract
                # batteries are handled on the contract side below.
                test_governed, classify_test_orphans = _discover_governed_test_modules(root, cfg)
                governed_by_owner = _governed_modules_by_owner(
                    workspace, governed_modules, test_governed
                )
                governed_union = set().union(*governed_by_owner.values())
                artifact_orphan_objs = _find_project_orphans(
                    root=root,
                    cfg=cfg,
                    source_dirs=source_dirs,
                    test_dirs=test_dirs,
                    governed_modules=governed_union,
                    governed_modules_by_owner=governed_by_owner,
                    contract_refs=None,
                    include_artifacts=True,
                    classify_test_orphans=classify_test_orphans,
                )

        # Contract-battery orphans gate whenever contracts are in scope — including
        # --contracts-only, where the magic block above did not run.
        if run_contracts:
            battery_orphan_objs = _find_project_orphans(
                root=root,
                cfg=cfg,
                source_dirs=source_dirs,
                test_dirs=test_dirs,
                governed_modules=set(),
                contract_refs={str(e.spec_ref) for e in specs.values()},
                include_artifacts=False,
            )

        magic_orphans = [str(o.path.relative_to(root)) for o in artifact_orphan_objs]
        battery_orphans_rel = [str(o.path.relative_to(root)) for o in battery_orphan_objs]

        magic_blocked = bool(magic_stale or magic_unbuilt or artifact_orphan_objs)
        contracts_blocked = bool(contract_blocked) or bool(battery_orphan_objs)
        blocked = (run_contracts and contracts_blocked) or (run_magic and magic_blocked)

        if json_mode:
            payload: dict[str, object] = {"command": "check", "ok": not blocked}
            if run_contracts:
                payload["blocked"] = contract_blocked
                payload["checked"] = contract_checked
                # Contract-battery orphans always live at the top level; a
                # combined check emits both this and magic.orphans.
                payload["orphans"] = sorted(battery_orphans_rel)
            if run_magic:
                payload["magic"] = {
                    "fresh": magic_fresh,
                    "stale": magic_stale,
                    "unbuilt": sorted(magic_unbuilt),
                    # Generated/stub/sidecar orphans only; batteries are top-level.
                    "orphans": sorted(magic_orphans),
                }
            _emit_json(payload)
        else:

            def _print_orphan(orphan) -> None:
                relpath = str(orphan.path.relative_to(root))
                print(
                    f"[BLOCK] orphaned artifact: {relpath} "
                    f"(spec {orphan.source_module} no longer exists) — "
                    "run 'jaunt clean --orphans' or restore the spec"
                )

            if run_contracts:
                if specs:
                    for r in contract_results:
                        mark = "BLOCK" if is_blocking(r.state) else "ok"
                        line = f"[{mark}] {r.spec_ref}: {r.state.value}"
                        if is_blocking(r.state):
                            line += f" — {BLOCKING_MESSAGE.get(r.state, '')}"
                        print(line)
                    print(
                        f"Contract check: {len(contract_results)} checked, "
                        f"{len(contract_blocked_results)} blocked."
                    )
                else:
                    print("Contract check: 0 contract function(s).")
                # Contract-battery orphans are reported on the contract side.
                for orphan in sorted(battery_orphan_objs, key=lambda o: str(o.path)):
                    _print_orphan(orphan)
            if run_magic:
                if magic_unbuilt or magic_stale or artifact_orphan_objs:
                    print(
                        f"Magic freshness: {len(magic_unbuilt)} unbuilt, {len(magic_stale)} stale."
                    )
                    for module_name in sorted(magic_unbuilt):
                        if module_name in newly_governed_modules:
                            print(
                                f"[BLOCK] {module_name}: unbuilt "
                                "(newly governed by module scan — first build)"
                            )
                        else:
                            print(f"[BLOCK] {module_name}: unbuilt")
                    for module_name, reason in sorted(magic_stale.items()):
                        print(f"[BLOCK] {module_name}: stale ({_render_stale_reason(reason)})")
                    for orphan in sorted(artifact_orphan_objs, key=lambda o: str(o.path)):
                        _print_orphan(orphan)
                    hint = _fingerprint_env_hint(cfg, magic_stale)
                    if hint:
                        print(hint, file=sys.stderr)
                else:
                    print("Magic freshness: all modules fresh.")

        return EXIT_PYTEST_FAILURE if blocked else EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "check", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY


def cmd_reconcile(args: argparse.Namespace) -> int:
    context = _typescript_command_context(args)
    if context is not None:
        root, cfg, mode = context
        if (failure := _target_dispatch_failure(args, mode)) is not None:
            return failure
        if mode == "ts":
            return _cmd_typescript_reconcile_loaded(args, root, cfg)
        if mode == "mixed":
            return _cmd_mixed_reconcile(args, root, cfg)

    json_mode = _is_json_mode(args)
    try:
        import importlib

        from jaunt import __version__

        root, cfg = _load_config(args)
        from jaunt.contract import runner
        from jaunt.contract.derive import extract_blocks_via_model

        _backend_box: list[GeneratorBackend] = []
        cost_tracker = _command_cost_tracker(args, cfg, "py")

        def _model_extract(prose: str, func_name: str = "f"):
            if not _backend_box:
                _backend_box.append(_command_backend(args, cfg, "py"))
            backend = _backend_box[0]

            async def _complete(system: str, user: str) -> str:
                text, usage = await backend.complete_text_with_usage(system=system, user=user)
                if usage is not None:
                    cost_tracker.record(f"contract:{func_name}", usage)
                    cost_tracker.check_budget()
                return text

            return asyncio.run(
                extract_blocks_via_model(prose, complete=_complete, func_name=func_name)
            )

        specs = _discover_contract_specs(root=root, cfg=cfg)
        target_mods = _iter_target_modules(getattr(args, "target", []) or [])

        results = []
        for entry in sorted(specs.values(), key=lambda e: str(e.spec_ref)):
            if target_mods and entry.module not in target_mods:
                continue
            module = importlib.import_module(entry.module)
            owner, source_roots = _contract_owner_context(root=root, cfg=cfg, module=entry.module)
            results.append(
                runner.reconcile_entry(
                    owner,
                    cfg.contract.battery_dir,
                    cfg.contract.derive,
                    cfg.contract.strength,
                    entry,
                    module_namespace=vars(module),
                    tool_version=__version__,
                    model_extract=_model_extract,
                    source_roots=source_roots,
                    property_max_examples=cfg.contract.property_max_examples,
                )
            )

        failed = [r for r in results if not r.ok]
        if json_mode:
            payload: dict[str, object] = {
                "command": "reconcile",
                "ok": not failed,
                "reconciled": [
                    {
                        "ref": r.spec_ref,
                        "strength": r.strength,
                        "strength_excluded": r.strength_excluded,
                        "wrote": r.wrote,
                    }
                    for r in results
                    if r.ok
                ],
                "failed": [{"ref": r.spec_ref, "failures": r.failures} for r in failed],
            }
            if getattr(args, "_mixed_runtime", None) is not None:
                payload["cost"] = _command_cost_summary(args, "py", cost_tracker)
            _emit_json(payload)
        else:
            for r in results:
                if r.ok:
                    excluded = r.strength_excluded
                    suffix = f" ({excluded} fixture/property cases not scored)" if excluded else ""
                    print(f"[ok] {r.spec_ref}: in sync (strength {r.strength}){suffix}")
                else:
                    print(f"[FAIL] {r.spec_ref}: body does not satisfy contract")
                    for f in r.failures:
                        print(f"    - {f}")
            print(f"Reconcile: {len(results) - len(failed)} ok, {len(failed)} failed.")

        return EXIT_PYTEST_FAILURE if failed else EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "reconcile", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY
    except JauntGenerationError as e:
        _print_error(e)
        if json_mode:
            payload = {"command": "reconcile", "ok": False, "error": str(e)}
            runtime = getattr(args, "_mixed_runtime", None)
            if runtime is not None:
                payload["cost"] = runtime.summary("py")
            _emit_json(payload)
        return EXIT_GENERATION_ERROR


def cmd_adopt(args: argparse.Namespace) -> int:
    context = _typescript_command_context(args)
    if context is not None:
        root, cfg, mode = context
        if (failure := _target_dispatch_failure(args, mode)) is not None:
            return failure
        if mode == "ts" or (
            mode == "mixed"
            and (str(getattr(args, "ref", "")).startswith("ts:") or "#" in str(args.ref))
        ):
            return _cmd_typescript_adopt_loaded(args, root, cfg)

    json_mode = _is_json_mode(args)
    try:
        import importlib

        from jaunt import __version__
        from jaunt.contract import runner
        from jaunt.contract.edits import add_contract_marker

        root, cfg = _load_config(args)
        ref = args.ref
        module, sep, func = ref.partition(":")
        if not sep:
            module, _, func = ref.rpartition(".")
        if not module or not func:
            raise JauntConfigError(f"adopt expects a 'module:func' ref, got {ref!r}.")
        if "." in func:
            _eprint(
                f"error: contract mode adopts the whole class: "
                f"jaunt adopt {module}:{func.split('.')[0]}"
            )
            return EXIT_CONFIG_OR_DISCOVERY

        src_path = _resolve_contract_source_file(root=root, cfg=cfg, module=module)
        source = src_path.read_text(encoding="utf-8")
        src_path.write_text(add_contract_marker(source, func), encoding="utf-8")

        # Re-import with the marker present and reconcile this one entry.
        specs = _discover_contract_specs(root=root, cfg=cfg)
        entry = next((e for e in specs.values() if e.module == module and e.qualname == func), None)
        if entry is None:
            raise JauntDiscoveryError(f"Adopted {ref!r} but could not re-discover it.")

        importlib.reload(importlib.import_module(module))
        mod = importlib.import_module(module)
        owner, source_roots = _contract_owner_context(root=root, cfg=cfg, module=module)
        result = runner.reconcile_entry(
            owner,
            cfg.contract.battery_dir,
            cfg.contract.derive,
            cfg.contract.strength,
            entry,
            module_namespace=vars(mod),
            tool_version=__version__,
            source_roots=source_roots,
            property_max_examples=cfg.contract.property_max_examples,
        )

        if result.ok:
            from jaunt import journal as _journal

            _journal.append_events(
                root,
                [
                    _journal.JournalEvent(
                        action="adopt",
                        module=result.spec_ref,
                        detail=f"battery derived (strength {result.strength})",
                    )
                ],
            )

        if json_mode:
            _emit_json(
                {
                    "command": "adopt",
                    "ok": result.ok,
                    "ref": result.spec_ref,
                    "strength": result.strength,
                    "strength_excluded": result.strength_excluded,
                    "failures": result.failures,
                }
            )
        elif result.ok:
            excluded = result.strength_excluded
            suffix = f" ({excluded} fixture/property cases not scored)" if excluded else ""
            print(f"Adopted {result.spec_ref} (strength {result.strength}){suffix}.")
        else:
            print(f"Adopted {result.spec_ref} but the body disagrees with its docstring:")
            for f in result.failures:
                print(f"    - {f}")

        return EXIT_OK if result.ok else EXIT_PYTEST_FAILURE
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "adopt", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY


def cmd_eject(args: argparse.Namespace) -> int:
    context = _typescript_command_context(args)
    if context is not None:
        root, cfg, mode = context
        if (failure := _target_dispatch_failure(args, mode)) is not None:
            return failure
        if mode == "ts" or (
            mode == "mixed"
            and (
                str(getattr(args, "ref", "")).startswith("ts:")
                or "#" in str(getattr(args, "ref", ""))
            )
        ):
            return _cmd_typescript_eject_loaded(args, root, cfg)

    json_mode = _is_json_mode(args)
    try:
        from jaunt.contract import runner
        from jaunt.contract.battery import de_jaunt_battery, parse_battery
        from jaunt.contract.edits import remove_contract_marker
        from jaunt.contract.strength import EJECT_STRENGTH_WARN, parse_strength

        root, cfg = _load_config(args)
        specs = _discover_contract_specs(root=root, cfg=cfg)

        if getattr(args, "all", False):
            targets = list(specs.values())
        else:
            ref = args.ref
            module, sep, func = ref.partition(":")
            if not sep:
                module, _, func = ref.rpartition(".")
            if "." in func:
                _eprint(
                    f"error: contract mode ejects the whole class: "
                    f"jaunt eject {module}:{func.split('.')[0]}"
                )
                return EXIT_CONFIG_OR_DISCOVERY
            targets = [e for e in specs.values() if e.module == module and e.qualname == func]
            if not targets:
                raise JauntDiscoveryError(f"No contract function matches {ref!r}.")

        ejected: list[str] = []
        warnings: list[str] = []
        for entry in targets:
            owner, _source_roots = _contract_owner_context(root=root, cfg=cfg, module=entry.module)
            path = runner.battery_path(owner, cfg.contract.battery_dir, entry)
            if path.is_file():
                parsed = parse_battery(path.read_text(encoding="utf-8"))
                strength = (parsed.header or {}).get("strength", "0/0")
                killed, applicable = parse_strength(strength)
                if applicable == 0 or killed / applicable < EJECT_STRENGTH_WARN:
                    warnings.append(
                        f"{entry.spec_ref}: weak contract (strength {strength}); "
                        "freezing weak tests."
                    )
                path.write_text(
                    de_jaunt_battery(
                        path.read_text(encoding="utf-8"),
                        provenance=f"was {entry.spec_ref}",
                    ),
                    encoding="utf-8",
                )
            src = Path(entry.source_file).read_text(encoding="utf-8")
            Path(entry.source_file).write_text(
                remove_contract_marker(src, entry.qualname), encoding="utf-8"
            )
            ejected.append(str(entry.spec_ref))

        if json_mode:
            _emit_json({"command": "eject", "ok": True, "ejected": ejected, "warnings": warnings})
        else:
            for w in warnings:
                print(f"warning: {w}")
            for ref in ejected:
                print(f"Ejected {ref} -> plain Python + plain pytest.")
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "eject", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY


def cmd_instructions(args: argparse.Namespace) -> int:
    """Print a project-aware agent primer for operating Jaunt.

    Always exits 0: outside an initialized project it prints the framework rules
    plus an "init" note instead of the live project section.
    """
    from jaunt import instructions

    json_mode = _is_json_mode(args)
    project: dict | None = None
    note: str | None = None
    try:
        root, cfg = _load_config(args)
    except JauntConfigError as e:
        note = instructions.no_project_note(str(e))
    else:
        try:
            project = instructions.project_section(root, cfg)
        except Exception as e:  # noqa: BLE001 - never let introspection break the primer
            note = (
                f"Project detected but could not be inspected "
                f"({type(e).__name__}); run `jaunt status`."
            )

    text = instructions.render(project=project, note=note)
    if json_mode:
        _emit_json({"command": "instructions", "ok": True, "text": text, "project": project})
    else:
        print(text)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    context = _typescript_command_context(args)
    if context is not None:
        root, cfg, mode = context
        if (failure := _target_dispatch_failure(args, mode)) is not None:
            return failure
        if mode == "ts":
            return _cmd_typescript_status_loaded(args, root, cfg)
        if mode == "mixed":
            return _cmd_mixed_status(args, root, cfg)

    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        magic_only = bool(getattr(args, "magic_only", False))
        include_target_tests = _effective_include_target_tests(cfg, args)
        build_instructions = _effective_build_instructions(cfg, args)

        from jaunt.workspace import resolve_workspace

        workspace = resolve_workspace(root, cfg)
        source_dirs = list(workspace.source_roots)
        _prepend_sys_path([*source_dirs, root])

        tree_drift = None
        if cfg.context.repo_map and not magic_only:
            from jaunt.repo_context import api as rc_api

            try:
                d = rc_api.check_drift(root=root, cfg=cfg)
                tree_drift = (
                    None
                    if d is None
                    else {
                        "added": len(d.added),
                        "removed": len(d.removed),
                        "restaled": len(d.restaled),
                    }
                )
            except Exception:  # noqa: BLE001
                tree_drift = None

        from jaunt.deps import build_spec_graph

        def _contract_rows(infer_default: bool) -> tuple[list[dict[str, object]], set[str]]:
            from jaunt.contract import runner as contract_runner
            from jaunt.contract.drift import DriftState

            contract_specs = _discover_contract_specs(root=root, cfg=cfg)
            rows: list[dict[str, object]] = []
            review: set[str] = set()
            if not contract_specs:
                return rows, review

            statuses = {}
            for entry in contract_specs.values():
                owner, source_roots = _contract_owner_context(
                    root=root, cfg=cfg, module=entry.module
                )

                def _run_battery(
                    path: Path,
                    *,
                    _owner=owner,
                    _source_roots=source_roots,
                ) -> bool:
                    return contract_runner.run_battery_file(
                        path,
                        root=_owner,
                        source_roots=_source_roots,
                    )

                statuses[str(entry.spec_ref)] = contract_runner.evaluate_entry(
                    owner,
                    cfg.contract.battery_dir,
                    cfg.contract.derive,
                    entry,
                    run_battery=_run_battery,
                )

            cgraph = build_spec_graph(contract_specs, infer_default=infer_default)
            stale_prose = {
                ref for ref, st in statuses.items() if st.state is DriftState.STALE_PROSE
            }
            for ref, deps in cgraph.items():
                if any(str(d) in stale_prose for d in deps):
                    review.add(str(ref))

            for ref in sorted(statuses):
                st = statuses[ref]
                rows.append(
                    {
                        "ref": ref,
                        "state": st.state.value,
                        "strength": st.strength or "0/0",
                        "strength_excluded": st.strength_excluded,
                        "review": ref in review,
                    }
                )
            return rows, review

        infer_default = bool(cfg.build.infer_deps) and (not bool(args.no_infer_deps))
        mstatus = compute_magic_status(
            root=root,
            cfg=cfg,
            source_dirs=source_dirs,
            build_instructions=build_instructions,
            include_target_tests=include_target_tests,
            infer_deps=infer_default,
            force=bool(args.force),
            target=args.target,
        )
        contract_rows: list[dict[str, object]] = []
        review_refs: set[str] = set()
        if not magic_only:
            contract_rows, review_refs = _contract_rows(infer_default)

        from jaunt import registry as _registry

        _governed = set(_registry.get_specs_by_module("magic").keys())
        _pkg_dir = next((d for d in source_dirs if d.exists()), None)
        _test_dirs = list(workspace.artifact_test_roots())
        _test_governed, _classify_test_orphans = _discover_governed_test_modules(root, cfg)
        _governed_by_owner = _governed_modules_by_owner(workspace, _governed, _test_governed)
        _governed_union = set().union(*_governed_by_owner.values())
        orphan_objs = (
            _find_project_orphans(
                root=root,
                cfg=cfg,
                source_dirs=source_dirs,
                test_dirs=_test_dirs,
                governed_modules=_governed_union,
                governed_modules_by_owner=_governed_by_owner,
                contract_refs=(None if magic_only else {str(r["ref"]) for r in contract_rows}),
                classify_test_orphans=_classify_test_orphans,
            )
            if _pkg_dir is not None
            else []
        )
        orphan_rels = [str(o.path.relative_to(root)) for o in orphan_objs]

        if mstatus.total == 0:
            if json_mode:
                payload: dict[str, object] = {
                    "command": "status",
                    "ok": True,
                    "stale": [],
                    "stale_changes": {},
                    "fresh": [],
                    "digests": mstatus.digests,
                    "orphans": orphan_rels,
                }
                if not magic_only:
                    payload.update(
                        {
                            "contracts": contract_rows,
                            "contract_review": sorted(review_refs),
                            "tree": tree_drift,
                        }
                    )
                _emit_json(payload)
            else:
                print("Status: 0 module(s) total")
                print("No magic specs discovered.")
                if contract_rows:
                    print(f"Contracts ({len(contract_rows)}):")
                    for row in contract_rows:
                        flag = " [review]" if row["review"] else ""
                        excluded = row["strength_excluded"]
                        suffix = (
                            f" ({excluded} fixture/property cases not scored)" if excluded else ""
                        )
                        print(
                            f"- {row['ref']}: {row['state']} (strength {row['strength']}){suffix}"
                            + flag
                        )
                if tree_drift is not None:
                    print(f"tree: {tree_drift} (run jaunt tree)")
                if orphan_rels:
                    print(f"Orphaned artifacts ({len(orphan_rels)}):")
                    for orphan in sorted(orphan_objs, key=lambda o: str(o.path)):
                        relpath = str(orphan.path.relative_to(root))
                        print(f"- {relpath} (spec {orphan.source_module} no longer exists)")
            return EXIT_OK

        stale = mstatus.stale
        fresh = mstatus.fresh
        stale_changes = mstatus.stale_changes

        if json_mode:
            payload = {
                "command": "status",
                "ok": True,
                "stale": sorted(stale),
                "stale_changes": stale_changes,
                "fresh": sorted(fresh),
                "digests": mstatus.digests,
                "orphans": orphan_rels,
            }
            if not magic_only:
                payload.update(
                    {
                        "contracts": contract_rows,
                        "contract_review": sorted(review_refs),
                        "tree": tree_drift,
                    }
                )
            _emit_json(payload)
        else:
            stale_sorted = sorted(stale)
            fresh_sorted = sorted(fresh)
            print(f"Status: {mstatus.total} module(s) total")
            print(f"Stale ({len(stale_sorted)}):")
            for mod in stale_sorted:
                print(f"- {mod} ({_render_stale_reason(stale_changes.get(mod, 'structural'))})")
            restamp_legacy = 0
            module_entries = _registry.get_specs_by_module("magic")
            for mod in stale_sorted:
                if stale_changes.get(mod) != "re-stamp":
                    continue
                try:
                    if any(
                        (
                            'raise RuntimeError("spec stub")'
                            in Path(entry.source_file).read_text(encoding="utf-8")
                        )
                        or (
                            "raise RuntimeError('spec stub')"
                            in Path(entry.source_file).read_text(encoding="utf-8")
                        )
                        for entry in module_entries.get(mod, [])
                    ):
                        restamp_legacy += 1
                except Exception:  # noqa: BLE001 - best-effort hint; status must not fail
                    continue
            if restamp_legacy:
                print(
                    f"hint: {restamp_legacy} module(s) are stale (re-stamp: free) "
                    "with legacy stub bodies — run 'jaunt migrate' to re-stamp them"
                )
            hint = _fingerprint_env_hint(cfg, stale_changes)
            if hint:
                print(hint, file=sys.stderr)
            print(f"Fresh ({len(fresh_sorted)}):")
            for mod in fresh_sorted:
                print(f"- {mod}")
            if orphan_rels:
                print(f"Orphaned artifacts ({len(orphan_rels)}):")
                for orphan in sorted(orphan_objs, key=lambda o: str(o.path)):
                    relpath = str(orphan.path.relative_to(root))
                    print(f"- {relpath} (spec {orphan.source_module} no longer exists)")
            if contract_rows:
                print(f"Contracts ({len(contract_rows)}):")
                for row in contract_rows:
                    flag = " [review]" if row["review"] else ""
                    excluded = row["strength_excluded"]
                    suffix = f" ({excluded} fixture/property cases not scored)" if excluded else ""
                    print(
                        f"- {row['ref']}: {row['state']} (strength {row['strength']}){suffix}"
                        + flag
                    )
            if tree_drift is not None:
                print(f"tree: {tree_drift} (run jaunt tree)")

        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "status", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1000}k"
    return str(n)


def _context_stats_summary_line(module: str, blocks: dict[str, dict[str, int]]) -> str:
    # The seeded-skills block is emitted under both `skills_workspace_seeded` and the
    # legacy `skills_workspace` alias (same value). Render only the seeded key — as
    # `skills(seeded)` — and never total or print the number twice.
    if "skills_workspace_seeded" in blocks:
        blocks = {
            ("skills(seeded)" if k == "skills_workspace_seeded" else k): v
            for k, v in blocks.items()
            if k != "skills_workspace"
        }
    total_chars = sum(b["chars"] for b in blocks.values())
    total_tokens = sum(b["est_tokens"] for b in blocks.values())
    parts: list[str] = []
    if total_chars > 0:
        ranked = sorted(blocks.items(), key=lambda kv: kv[1]["chars"], reverse=True)
        for name, b in ranked:
            if b["chars"] <= 0:
                continue
            pct = round(100 * b["chars"] / total_chars)
            if pct <= 0:
                continue
            parts.append(f"{name} {pct}%")
            if len(parts) >= 4:
                break
    breakdown = ", ".join(parts) if parts else "empty"
    return (
        f"{module} context: {_fmt_count(total_chars)} chars "
        f"(~{_fmt_count(total_tokens)} tok) — {breakdown}"
    )


async def _cmd_build_async(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        _maybe_load_dotenv(root)
        _sync_generated_dir_env(cfg)
        include_target_tests = _effective_include_target_tests(cfg, args)
        build_instructions = _effective_build_instructions(cfg, args)

        from jaunt.workspace import resolve_workspace

        workspace = resolve_workspace(root, cfg)
        source_dirs = list(workspace.source_roots)

        builtin_on = bool(cfg.skills.builtin) and not bool(
            getattr(args, "no_builtin_skills", False)
        )
        builtin_skill_names = tuple(cfg.skills.builtin_skills) if builtin_on else ()
        auto_skills_on = bool(cfg.skills.auto) and not bool(getattr(args, "no_auto_skills", False))
        if auto_skills_on and getattr(args, "_mixed_runtime", None) is not None:
            # Auto-skill elaboration owns its own Codex executor and predates
            # command-level runtime injection.  Defer missing/updated skills in
            # mixed mode rather than letting those calls escape the shared jobs
            # and budget boundary.  Existing seeded skills remain available.
            _eprint(
                "warn: deferred automatic PyPI skill generation for this mixed-target "
                "command; run `jaunt build --language py` to refresh external-library skills"
            )
        elif auto_skills_on:
            try:
                from jaunt import skills_auto

                skills_res = await skills_auto.ensure_pypi_skills(
                    project_root=root,
                    source_roots=[d for d in source_dirs if d.exists()],
                    generated_dir=cfg.paths.generated_dir,
                    llm=cfg.llm,
                    agent=cfg.agent,
                    codex=cfg.codex,
                    skills=cfg.skills,
                )
                for w in skills_res.warnings:
                    _eprint(f"warn: {w}")
            except Exception as e:  # noqa: BLE001 - best-effort; never block build
                _eprint(f"warn: failed ensuring external library skills: {type(e).__name__}: {e}")

        repo_map_block = ""
        if cfg.context.repo_map and not bool(getattr(args, "no_repo_map", False)):
            from jaunt.repo_context import api as rc_api

            precomputed_repo_map = getattr(args, "_mixed_repo_map_block", None)
            if isinstance(precomputed_repo_map, str):
                repo_map_block = precomputed_repo_map
            elif cfg.context.enrich and getattr(args, "_mixed_runtime", None) is not None:
                # Repo-map enrichment also owns a legacy standalone Codex
                # backend.  Keep AST repo-map maintenance enabled, but defer
                # enrichment so no unmetered model call escapes mixed runtime.
                _eprint(
                    "warn: deferred model-enriched repo-map descriptions for this "
                    "mixed-target command; run `jaunt tree --enrich` separately"
                )
                try:
                    from jaunt.repo_context import block as rc_block

                    repo_map_doc, _ = rc_api.sync_tree(
                        root=root,
                        cfg=cfg,
                        today=_today(),
                        enrich=False,
                    )
                    repo_map_block = rc_block.render_repo_map(
                        repo_map_doc,
                        max_chars=cfg.context.max_chars,
                    )
                except Exception:  # noqa: BLE001 - repo map remains best-effort
                    repo_map_block = ""
            else:
                repo_map_block = rc_api.repo_map_block_for_build(
                    root=root,
                    cfg=cfg,
                    today=_today(),
                )

        from jaunt.skill_seed import skills_fingerprint

        build_skills_digest = skills_fingerprint(
            project_root=root, builtin_names=builtin_skill_names
        )

        _prepend_sys_path([*source_dirs, root])

        from jaunt import discovery, registry
        from jaunt.deps import build_spec_graph, collapse_to_module_dag, find_cycles

        modules = [route.module for route in workspace.modules]
        discovery.prepare_import_environment(
            module_names=modules,
            roots=[d for d in source_dirs if d.exists()],
        )
        discovery.import_and_collect(modules, kind="magic")
        static_targeted_test_entries = (
            _discover_static_targeted_test_entries(root=root, cfg=cfg)
            if include_target_tests
            else []
        )

        specs = dict(registry.get_magic_registry())
        if not specs:
            if json_mode:
                _emit_json(
                    {
                        "command": "build",
                        "ok": True,
                        "generated": [],
                        "skipped": [],
                        "refrozen": [],
                        "failed": {},
                    }
                )
            return EXIT_OK

        infer_default = bool(cfg.build.infer_deps) and (not bool(args.no_infer_deps))
        spec_graph = build_spec_graph(specs, infer_default=infer_default)
        module_dag = collapse_to_module_dag(spec_graph)

        # Early cycle detection with actionable diagnostics.
        cycles = find_cycles(spec_graph)
        if cycles:
            _eprint("error: dependency cycle(s) detected")
            for cycle in cycles:
                path = " -> ".join(str(s) for s in cycle) + " -> " + str(cycle[0])
                _eprint(f"  {path}")
            _eprint("hint: break the cycle by removing a dep from one of these specs")
            raise JauntDependencyCycleError(
                "Dependency cycle detected: "
                + ", ".join(" -> ".join(str(s) for s in c) for c in cycles)
            )

        module_specs = registry.get_specs_by_module("magic")

        # Created up front so a (best-effort) project-overview model call is charged
        # against the same budget/summary as the per-module build calls below.
        cost_tracker = _command_cost_tracker(args, cfg, "py")
        backend = _command_backend(args, cfg, "py")

        overview_block = ""
        if cfg.context.overview:
            from jaunt.repo_context import overview as rc_overview

            overview_block = await rc_overview.project_overview_block_for_build(
                root=root,
                cfg=cfg,
                module_specs=module_specs,
                repo_map_block=repo_map_block,
                backend=backend,
                cost_tracker=cost_tracker,
            )
            # Abort early if generating the overview already blew the budget.
            cost_tracker.check_budget()

        package_dir = source_dirs[0]

        # Lazy import so other work can land independently.
        from jaunt import builder
        from jaunt.generation_fingerprint import generation_fingerprint
        from jaunt.module_api import module_api_digest
        from jaunt.module_contract import group_test_entries_by_target_module

        build_generation_fingerprint = generation_fingerprint(
            cfg,
            kind="build",
            build_instructions=build_instructions,
            include_target_tests=include_target_tests,
        )
        build_module_context_digests: dict[str, str] = {}
        build_module_api_digests: dict[str, str] = {}
        build_module_base_api_digests: dict[str, str] = {}
        targeted_test_entries = group_test_entries_by_target_module(static_targeted_test_entries)
        for module_name, entries in module_specs.items():
            module_dir = workspace.route_for(module_name).output_base
            expected, _errs = builder._build_expected_names(entries)
            wcc = builder._whole_class_context(
                entries,
                specs=specs,
                package_dir=module_dir,
                generated_dir=cfg.paths.generated_dir,
                module_output_bases=workspace.output_bases,
            )
            build_module_context_digests[module_name] = builder.build_module_context_artifacts(
                module_name=module_name,
                entries=entries,
                expected_names=expected,
                module_specs=module_specs,
                module_dag=module_dag,
                package_dir=module_dir,
                generated_dir=cfg.paths.generated_dir,
                build_instructions=build_instructions,
                targeted_test_entries=targeted_test_entries,
                base_contract_block=wcc.base_contract_block,
                whole_class_contract_block=wcc.whole_class_contract_block,
                inherited_api_block=wcc.inherited_api_block,
            ).digest
            build_module_api_digests[module_name] = module_api_digest(entries)
            build_module_base_api_digests[module_name] = wcc.base_api_digest
        stale = builder.detect_stale_modules(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            generation_fingerprint=build_generation_fingerprint,
            module_context_digests=build_module_context_digests,
            module_base_api_digests=build_module_base_api_digests,
            module_output_bases=workspace.output_bases,
            force=bool(args.force),
        )
        api_changed = builder.detect_api_changed_modules(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            module_api_digests=build_module_api_digests,
            module_output_bases=workspace.output_bases,
        )

        target_mods = _iter_target_modules(args.target)
        allowed_modules: set[str] | None = None
        if target_mods:
            allowed = _deps_closure(target_mods, module_dag=module_dag)
            allowed_modules = allowed
            stale = {m for m in stale if m in allowed}
            api_changed = {m for m in api_changed if m in allowed}

        expanded_stale = builder.expand_stale_modules(
            module_dag,
            stale,
            changed_modules=api_changed,
            allowed_modules=allowed_modules,
        )
        refrozen_modules: set[str] = set()
        if (not bool(args.force)) and expanded_stale:
            try:
                module_digest_fn = builder.module_digest
            except AttributeError:
                from jaunt.digest import module_digest as module_digest_fn
            from jaunt.digest import legacy_module_digest

            base_api_changed: set[str] = set()
            header_fields_by_module: dict[str, dict[str, object]] = {}
            for module_name in expanded_stale:
                entries = module_specs.get(module_name)
                if entries is None:
                    continue
                header_fields_by_module[module_name] = {
                    "tool_version": builder._tool_version(),
                    "kind": "build",
                    "source_module": module_name,
                    "module_digest": module_digest_fn(module_name, entries, specs, spec_graph),
                    "legacy_module_digest": legacy_module_digest(
                        module_name, entries, specs, spec_graph
                    ),
                    "generation_fingerprint": build_generation_fingerprint,
                    "module_context_digest": build_module_context_digests.get(module_name, ""),
                    "module_api_digest": module_api_digest(entries),
                    "spec_refs": [str(e.spec_ref) for e in entries],
                }
                fresh_base = build_module_base_api_digests.get(module_name)
                if fresh_base:
                    existing_src = builder._read_generated(
                        package_dir,
                        cfg.paths.generated_dir,
                        module_name,
                        module_output_bases=workspace.output_bases,
                    )
                    if existing_src is not None:
                        on_disk_base = builder._normalize_digest(
                            builder.extract_base_api_digest(existing_src)
                        )
                        if on_disk_base is None or on_disk_base != builder._normalize_digest(
                            fresh_base
                        ):
                            base_api_changed.add(module_name)
            plan = await builder.plan_refreeze_or_rebuild(
                package_dir=package_dir,
                generated_dir=cfg.paths.generated_dir,
                module_specs=module_specs,
                specs=specs,
                spec_graph=spec_graph,
                module_dag=module_dag,
                stale_modules=expanded_stale & set(module_specs.keys()),
                header_fields_by_module=header_fields_by_module,
                base_api_changed=base_api_changed,
                cfg=cfg.semantic_gate,
                gate_enabled=cfg.semantic_gate.enabled
                and not bool(getattr(args, "no_semantic_gate", False)),
                run_exec=_command_semantic_exec(args),
                module_output_bases=workspace.output_bases,
            )
            # The Python semantic gate predates cost reporting.  Mixed commands
            # charge its direct Codex usage to the outer ledger, then enforce
            # that shared ceiling here even though the gate itself fails closed.
            cost_tracker.check_budget()
            refrozen_modules = set(plan.refrozen)
            expanded_stale = set(plan.rebuild)
            # The planner already rolled MEANINGFUL verdicts up the dependency
            # graph into `plan.rebuild`. Drop re-frozen modules from the API-changed
            # set so run_build's own dependent expansion cannot resurrect a module
            # whose only change was judged EQUIVALENT (semantic caching).
            api_changed = api_changed - refrozen_modules
        stale = expanded_stale
        progress = _make_progress(
            args,
            label="build",
            total=len(expanded_stale),
            json_mode=json_mode,
        )

        from jaunt.cache import ResponseCache

        cache_dir = root / ".jaunt" / "cache"
        no_cache = bool(getattr(args, "no_cache", False))
        response_cache = ResponseCache(cache_dir, enabled=not no_cache)
        # cost_tracker was created up front (above) so the project-overview model call
        # is charged against the same budget and cost summary as the build calls.

        search_enabled = cfg.context.search.enabled and cfg.context.search.internal_retrieval
        if cfg.context.search.enabled:
            from jaunt.repo_context import search as rc_search

            rc_search.ensure_index(root)

        newly_governed = _newly_governed_for_workspace(
            list(specs.values()),
            workspace=workspace,
            generated_dir=cfg.paths.generated_dir,
        )
        if newly_governed and not json_mode:
            for mod in sorted(newly_governed):
                for sym in newly_governed[mod]:
                    print(f"newly governed by module scan: {mod}.{sym} — first build")

        jobs = int(args.jobs) if args.jobs is not None else int(cfg.build.jobs)
        report = await builder.run_build(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=stale,
            changed_modules=api_changed,
            allowed_modules=allowed_modules,
            backend=backend,
            generation_fingerprint=build_generation_fingerprint,
            repo_map_block=repo_map_block,
            project_overview_block=overview_block,
            search_enabled=search_enabled,
            search_max_hits=cfg.context.search.max_hits,
            source_roots=[d for d in source_dirs if d.exists()],
            module_output_bases=workspace.output_bases,
            module_owner_dirs={route.module: route.owner_dir for route in workspace.modules},
            jobs=jobs,
            progress=progress,
            response_cache=response_cache,
            cost_tracker=cost_tracker,
            ty_retry_attempts=cfg.build.ty_retry_attempts,
            async_runner=cfg.build.async_runner,
            build_instructions=build_instructions,
            check_generated_imports=cfg.build.check_generated_imports,
            generated_import_allowlist=cfg.build.generated_import_allowlist,
            targeted_test_entries=targeted_test_entries,
            project_root=root,
            builtin_skill_names=builtin_skill_names,
            skills_digest=build_skills_digest,
            emit_stubs=cfg.build.emit_stubs,
            build_preamble_override=cfg.prompts.build_preamble or None,
        )

        if report.failed and not json_mode:
            _eprint(format_build_failures(report.failed))

        if report.needs_deps and not json_mode:
            for mod, markers in sorted(report.needs_deps.items()):
                _eprint(
                    f"warning: {mod} inlined logic for undeclared dependencies "
                    f"({len(markers)}); declare the dep(s) to reuse them:"
                )
                for marker in markers:
                    _eprint(f"  {marker}")

        if report.advisories and not json_mode:
            print("Advisories (from the generation agent — informational):")
            for mod, items in sorted(report.advisories.items()):
                for item in items:
                    print(f"  {mod}: {item}")

        if report.stub_warnings and not json_mode:
            for warning in report.stub_warnings:
                _eprint(warning)

        if report.emitted_stubs and not json_mode:
            print(f"Emitted {len(report.emitted_stubs)} .pyi stub(s).")

        from jaunt import journal as _journal

        events = []
        for mod in sorted(report.generated):
            events.append(_journal.JournalEvent(action="build", module=mod, detail="rebuilt"))
        for mod in sorted(refrozen_modules):
            events.append(
                _journal.JournalEvent(
                    action="refreeze", module=mod, detail="cosmetic (gate: EQUIVALENT)"
                )
            )
        for mod, err in sorted(report.failed.items()):
            first = str(err).splitlines()[0][:120] if str(err) else "generation failed"
            events.append(_journal.JournalEvent(action="build-fail", module=mod, detail=first))
        for mod, items in sorted(report.advisories.items()):
            for item in items:
                events.append(
                    _journal.JournalEvent(
                        action="advisory", module=mod, detail=" ".join(item.split())
                    )
                )
        _journal.append_events(root, events)

        if not json_mode and (cost_tracker.api_calls > 0 or cost_tracker.cache_hits > 0):
            _eprint(cost_tracker.format_summary())

        if not json_mode:
            for mod in sorted(report.generated):
                blocks = report.context_stats.get(mod)
                if blocks:
                    print(_context_stats_summary_line(mod, blocks))
            summary = f"Built {len(report.generated)} module(s), skipped {len(report.skipped)}"
            if report.failed:
                summary += f", {len(report.failed)} failed"
            print(f"{summary}.")

        if json_mode:
            build_payload: dict[str, object] = {
                "command": "build",
                "ok": not report.failed,
                "generated": sorted(report.generated),
                "skipped": sorted(report.skipped),
                "refrozen": sorted(refrozen_modules),
                "failed": {k: v for k, v in sorted(report.failed.items())},
                "cost": _command_cost_summary(args, "py", cost_tracker),
                "cache": {"hits": response_cache.hits, "misses": response_cache.misses},
                "context_stats": {k: v for k, v in sorted(report.context_stats.items())},
            }
            if report.needs_deps:
                build_payload["needs_deps"] = {k: v for k, v in sorted(report.needs_deps.items())}
            if report.advisories:
                build_payload["advisories"] = {
                    k: list(v) for k, v in sorted(report.advisories.items())
                }
            if report.emitted_stubs:
                build_payload["emitted_stubs"] = {
                    k: v for k, v in sorted(report.emitted_stubs.items())
                }
            if report.stub_warnings:
                build_payload["stub_warnings"] = report.stub_warnings
            if newly_governed:
                build_payload["newly_governed"] = {k: v for k, v in sorted(newly_governed.items())}
            _emit_json(build_payload)

        if report.failed:
            return EXIT_GENERATION_ERROR
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "build", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY
    except (JauntGenerationError, ImportError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "build", "ok": False, "error": str(e)})
        return EXIT_GENERATION_ERROR


def cmd_build(args: argparse.Namespace) -> int:
    context = _typescript_command_context(args)
    if context is not None:
        root, cfg, mode = context
        if (failure := _target_dispatch_failure(args, mode)) is not None:
            return failure
        if mode == "ts":
            return _cmd_typescript_build_loaded(args, root, cfg)
        if mode == "mixed":
            return _cmd_mixed_build(args, root, cfg)
    return asyncio.run(_cmd_build_async(args))


async def _cmd_test_async(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        _maybe_load_dotenv(root)
        _sync_generated_dir_env(cfg)
        if bool(getattr(args, "no_redact_derived", False)):
            _eprint(
                "WARNING: --no-redact-derived feeds full held-out (derived-tier) failure "
                "detail — expected values and tracebacks — into the Implementer's repair "
                "context. This DEFEATS the held-out barrier and is for debugging only."
            )
        include_target_tests = _effective_include_target_tests(cfg, args)
        build_instructions = _effective_build_instructions(cfg, args)

        # Fail fast BEFORE spending any tokens (build or test generation) when
        # pytest will be needed to run the generated tests but is not installed.
        if not bool(args.no_run):
            from jaunt import tester

            tester.ensure_pytest_available()

        from jaunt.workspace import resolve_workspace

        workspace = resolve_workspace(root, cfg)
        source_dirs = list(workspace.source_roots)
        owner_scope_raw = getattr(args, "_workspace_owner", None)
        owner_scope = Path(owner_scope_raw).resolve() if owner_scope_raw else None
        test_routes = [
            route
            for route in workspace.test_roots
            if owner_scope is None or route.owner_dir == owner_scope
        ]
        test_dirs = [route.root for route in test_routes]
        test_project_dir = owner_scope or (
            test_routes[0].owner_dir
            if test_routes and len({route.owner_dir for route in test_routes}) == 1
            else root
        )
        # Import source specs and namespace-package test modules without
        # prepending raw test roots, which can shadow stdlib/dependency imports.
        _prepend_sys_path([*source_dirs, test_project_dir, root])

        if not bool(args.no_build):
            rc = await _cmd_build_async(args)
            if rc != EXIT_OK:
                return rc

        from jaunt import discovery, paths, registry
        from jaunt.deps import build_spec_graph, collapse_to_module_dag
        from jaunt.module_api import (
            build_dependency_api_block,
            build_generated_class_api_summary,
            generated_public_api_digest,
        )
        from jaunt.module_contract import (
            build_module_contract,
            group_test_entries_by_target_module,
            synthesize_auto_class_test_entries,
            target_refs_by_test_name,
        )

        # Provide production API reference material (from @jaunt.magic) so
        # test generation can import the real APIs instead of guessing module names.
        magic_dependency_apis: dict[SpecRef, str] = {}
        build_magic_specs: dict[SpecRef, registry.SpecEntry] = {}
        build_module_specs: dict[str, list[registry.SpecEntry]] = {}
        build_magic_spec_graph: dict[SpecRef, set[SpecRef]] = {}
        build_magic_module_dag: dict[str, set[str]] = {}
        if bool(args.no_build):
            src_mods = [route.module for route in workspace.modules]
            discovery.prepare_import_environment(
                module_names=src_mods,
                roots=[d for d in source_dirs if d.exists()],
            )
            discovery.import_and_collect(src_mods, kind="magic")
            build_magic_specs = dict(registry.get_magic_registry())
            build_module_specs = registry.get_specs_by_module("magic")
            build_magic_spec_graph = build_spec_graph(
                build_magic_specs,
                infer_default=bool(cfg.build.infer_deps) and (not bool(args.no_infer_deps)),
            )
            build_magic_module_dag = collapse_to_module_dag(build_magic_spec_graph)
        else:
            # cmd_build() already imported and registered magic specs.
            build_magic_specs = dict(registry.get_magic_registry())
            build_module_specs = registry.get_specs_by_module("magic")
            build_magic_spec_graph = build_spec_graph(
                build_magic_specs,
                infer_default=bool(cfg.build.infer_deps) and (not bool(args.no_infer_deps)),
            )
            build_magic_module_dag = collapse_to_module_dag(build_magic_spec_graph)

        package_dir = next((d for d in source_dirs if d.exists()), root)

        def _is_whole_class_magic(entry: registry.SpecEntry) -> bool:
            return (
                entry.class_name is None
                and "." not in entry.qualname
                and isinstance(entry.obj, type)
            )

        def _generated_source_for_magic(entry: registry.SpecEntry) -> str | None:
            try:
                generated_module = paths.spec_module_to_generated_module(
                    entry.module,
                    generated_dir=cfg.paths.generated_dir,
                )
                relpath = paths.generated_module_to_relpath(
                    generated_module,
                    generated_dir=cfg.paths.generated_dir,
                )
                generated_path = workspace.route_for(entry.module).output_base / relpath
                if not generated_path.exists():
                    return None
                return generated_path.read_text(encoding="utf-8")
            except Exception:
                return None

        magic_dependency_apis = {
            ref: build_dependency_api_block(entry) for ref, entry in build_magic_specs.items()
        }
        magic_target_api_digests: dict[SpecRef, str] = {}
        for ref, entry in build_magic_specs.items():
            if not _is_whole_class_magic(entry):
                continue
            generated_source = _generated_source_for_magic(entry)
            if generated_source is None:
                continue
            try:
                magic_dependency_apis[ref] = build_generated_class_api_summary(
                    generated_source,
                    entry.qualname,
                    spec_docstring=getattr(entry.obj, "__doc__", "") or "",
                    public_api_only=True,
                ).to_prompt_block()
                magic_target_api_digests[ref] = generated_public_api_digest(
                    generated_source,
                    entry.qualname,
                )
            except Exception:
                continue

        modules_set: set[str] = set()
        existing_test_dirs = [d for d in test_dirs if d.exists()]
        primary_test_route = (
            test_routes[0] if test_routes else workspace.primary_test_root(test_project_dir)
        )
        tests_package = primary_test_route.module_prefix
        for route in test_routes:
            mods = discovery.discover_modules(
                roots=[route.root],
                exclude=[],
                generated_dir=cfg.paths.generated_dir,
                module_prefix=route.module_prefix,
            )
            modules_set.update(mods)
        modules = sorted(modules_set)
        discovery.prepare_import_environment(module_names=modules, roots=existing_test_dirs)
        discovery.import_and_collect(modules, kind="test")

        specs = dict(registry.get_test_registry())
        auto_magic_specs = (
            {
                ref: entry
                for ref, entry in build_magic_specs.items()
                if workspace.route_for(entry.module).owner_dir == owner_scope
            }
            if owner_scope is not None
            else build_magic_specs
        )
        auto_entries = synthesize_auto_class_test_entries(
            auto_magic_specs,
            default_on=bool(cfg.test.auto_class_tests),
            tests_package=tests_package,
            generated_dir=cfg.paths.generated_dir,
        )
        for entries in auto_entries.values():
            for entry in entries:
                specs[entry.spec_ref] = entry
        if not specs:
            if json_mode:
                _emit_json({"command": "test", "ok": True, "exit_code": 0, "refrozen": []})
            return EXIT_OK
        targeted_test_entries = group_test_entries_by_target_module(list(specs.values()))
        if include_target_tests:
            build_targeted_test_entries = {
                module_name: [entry for entry in entries if ".__auto__." not in entry.module]
                for module_name, entries in targeted_test_entries.items()
            }
        else:
            build_targeted_test_entries = {}

        infer_default = bool(cfg.test.infer_deps) and (not bool(args.no_infer_deps))
        spec_graph = build_spec_graph(specs, infer_default=infer_default)
        module_dag = collapse_to_module_dag(spec_graph)
        module_specs = registry.get_specs_by_module("test")
        for module_name, entries in auto_entries.items():
            module_specs.setdefault(module_name, []).extend(entries)
            module_specs[module_name].sort(key=lambda e: (e.qualname, str(e.spec_ref)))

        # Lazy imports (these are layered; keep CLI import-time minimal).
        from jaunt import builder, tester
        from jaunt.generation_fingerprint import generation_fingerprint

        jobs = int(args.jobs) if args.jobs is not None else int(cfg.test.jobs)
        pytest_args = [*cfg.test.pytest_args, *list(args.pytest_args or [])]
        test_generation_fingerprint = generation_fingerprint(cfg, kind="test")
        test_module_context_digests: dict[str, str] = {}
        test_target_api_digests: dict[str, str] = {}
        for module_name, entries in module_specs.items():
            expected, _errs = builder._build_expected_names(entries)
            test_module_context_digests[module_name] = build_module_contract(
                entries=entries,
                expected_names=expected,
            ).digest
            target_digest_parts: set[str] = set()
            for refs in target_refs_by_test_name(entries).values():
                for ref in refs:
                    api_digest = magic_target_api_digests.get(ref)
                    if api_digest:
                        target_digest_parts.add(f"{ref}={api_digest}")
            if target_digest_parts:
                payload = "\n".join(sorted(target_digest_parts)).encode()
                test_target_api_digests[module_name] = hashlib.sha256(payload).hexdigest()

        stale = tester.detect_stale_test_modules(
            project_dir=test_project_dir,
            generated_dir=cfg.paths.generated_dir,
            tests_package=tests_package,
            test_roots=existing_test_dirs,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            generation_fingerprint=test_generation_fingerprint,
            module_context_digests=test_module_context_digests,
            target_api_digests=test_target_api_digests or None,
            force=bool(args.force),
        )
        stale = builder.expand_stale_modules(module_dag, stale)

        target_mods = _iter_target_modules(args.target)
        if target_mods:
            allowed = _deps_closure(target_mods, module_dag=module_dag)
            stale = {m for m in stale if m in allowed}

        test_refrozen_modules: set[str] = set()
        if (not bool(args.force)) and stale:
            test_header_fields_by_module: dict[str, dict[str, object]] = {}
            for module_name in stale:
                entries = module_specs.get(module_name)
                if entries is None:
                    continue
                test_module_context_digest = test_module_context_digests.get(module_name, "")
                if test_target_api_digests:
                    test_module_context_digest = (
                        tester.combine_module_context_digest(
                            test_module_context_digest,
                            test_target_api_digests.get(module_name),
                        )
                        or test_module_context_digest
                    )
                test_header_fields_by_module[module_name] = {
                    "tool_version": builder._tool_version(),
                    "kind": "test",
                    "source_module": module_name,
                    "module_digest": tester._test_module_digest(
                        module_name,
                        entries,
                        specs,
                        spec_graph,
                    ),
                    "legacy_module_digest": tester._legacy_test_module_digest(
                        module_name,
                        entries,
                        specs,
                        spec_graph,
                    ),
                    "generation_fingerprint": test_generation_fingerprint,
                    "module_context_digest": test_module_context_digest,
                    "spec_refs": [str(e.spec_ref) for e in entries],
                }
            test_plan = await tester.plan_test_refreeze_or_rebuild(
                project_dir=test_project_dir,
                generated_dir=cfg.paths.generated_dir,
                module_specs=module_specs,
                specs=specs,
                spec_graph=spec_graph,
                module_dag=module_dag,
                stale_modules=stale & set(module_specs.keys()),
                header_fields_by_module=test_header_fields_by_module,
                cfg=cfg.semantic_gate,
                tests_package=tests_package,
                test_roots=existing_test_dirs,
                gate_enabled=cfg.semantic_gate.enabled
                and not bool(getattr(args, "no_semantic_gate", False)),
                run_exec=_command_semantic_exec(args),
            )
            _check_shared_command_budget(args, "py")
            test_refrozen_modules = set(test_plan.refrozen)
            stale = set(test_plan.rebuild)

        total = len(stale & set(module_specs.keys()))
        progress = _make_progress(args, label="test", total=total, json_mode=json_mode)

        from jaunt.cache import ResponseCache

        cache_dir = root / ".jaunt" / "cache"
        no_cache = bool(getattr(args, "no_cache", False))
        response_cache = ResponseCache(cache_dir, enabled=not no_cache)
        cost_tracker = _command_cost_tracker(args, cfg, "py")
        backend = _command_backend(args, cfg, "py")

        build_generation_fingerprint = generation_fingerprint(
            cfg,
            kind="build",
            build_instructions=build_instructions,
            include_target_tests=include_target_tests,
        )
        builtin_on = bool(cfg.skills.builtin) and not bool(
            getattr(args, "no_builtin_skills", False)
        )
        builtin_skill_names = tuple(cfg.skills.builtin_skills) if builtin_on else ()
        from jaunt.skill_seed import skills_fingerprint

        test_skills_digest = skills_fingerprint(
            project_root=root, builtin_names=builtin_skill_names
        )
        repair_build_context = tester.RepairBuildContext(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=build_module_specs,
            specs=build_magic_specs,
            spec_graph=build_magic_spec_graph,
            module_dag=build_magic_module_dag,
            backend=backend,
            generation_fingerprint=build_generation_fingerprint,
            targeted_test_entries=build_targeted_test_entries,
            project_root=root,
            builtin_skill_names=builtin_skill_names,
            skills_digest=test_skills_digest,
            source_roots=[d for d in source_dirs if d.exists()],
            module_output_bases=workspace.output_bases,
            module_owner_dirs={route.module: route.owner_dir for route in workspace.modules},
            jobs=int(cfg.build.jobs),
            async_runner=cfg.build.async_runner,
            build_instructions=build_instructions,
            check_generated_imports=cfg.build.check_generated_imports,
            generated_import_allowlist=cfg.build.generated_import_allowlist,
        )

        result = tester.run_tests(
            project_dir=test_project_dir,
            tests_package=tests_package,
            generated_dir=cfg.paths.generated_dir,
            test_roots=existing_test_dirs,
            dependency_apis=magic_dependency_apis,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=stale,
            backend=backend,
            generation_fingerprint=test_generation_fingerprint,
            target_api_digests=test_target_api_digests or None,
            jobs=jobs,
            no_generate=False,
            no_run=bool(args.no_run),
            pytest_args=pytest_args,
            progress=progress,
            pythonpath=[*source_dirs, root],
            cwd=test_project_dir,
            response_cache=response_cache,
            cost_tracker=cost_tracker,
            async_runner=cfg.build.async_runner,
            repair_build_context=repair_build_context,
            no_redact_derived=bool(getattr(args, "no_redact_derived", False)),
            project_root=root,
            builtin_skill_names=builtin_skill_names,
            skills_digest=test_skills_digest,
        )

        if asyncio.iscoroutine(result):
            result = await result

        exit_code = int(getattr(result, "exit_code", 1))

        gen_failed = getattr(result, "generation_failed", {})
        if gen_failed and not json_mode:
            _eprint(format_test_generation_failures(gen_failed))

        if not json_mode:
            print(
                f"Generated {len(result.generated)} test module(s), skipped {len(result.skipped)}."
            )
            if getattr(result, "advisories", {}):
                print("Advisories (from the generation agent — informational):")
                for mod, items in sorted(result.advisories.items()):
                    for item in items:
                        print(f"  {mod}: {item}")

        from jaunt import journal as _journal

        _journal.append_events(
            root,
            [
                _journal.JournalEvent(action="advisory", module=mod, detail=" ".join(item.split()))
                for mod, items in sorted(getattr(result, "advisories", {}).items())
                for item in items
            ],
        )

        if json_mode:
            test_payload: dict[str, object] = {
                "command": "test",
                "ok": exit_code == 0 and not gen_failed,
                "exit_code": exit_code,
                "refrozen": sorted(test_refrozen_modules),
                "generation_failed": {k: v for k, v in sorted(gen_failed.items())},
            }
            if getattr(args, "_mixed_runtime", None) is not None:
                test_payload["cost"] = _command_cost_summary(args, "py", cost_tracker)
                test_payload["generated"] = sorted(result.generated)
                test_payload["skipped"] = sorted(result.skipped)
            if getattr(result, "advisories", None):
                test_payload["advisories"] = {
                    k: list(v) for k, v in sorted(result.advisories.items())
                }
            _emit_json(test_payload)

        if gen_failed or exit_code == EXIT_GENERATION_ERROR:
            return EXIT_GENERATION_ERROR
        if exit_code == 0:
            return EXIT_OK
        return EXIT_PYTEST_FAILURE
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "test", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY
    except (JauntGenerationError, ImportError, AttributeError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "test", "ok": False, "error": str(e)})
        return EXIT_GENERATION_ERROR


async def _cmd_test_workspace_async(args: argparse.Namespace) -> int:
    # Each owning pyproject is an independent pytest/import namespace.  Running
    # owners sequentially prevents identical ``tests.*`` module names from
    # colliding and lets package-local pytest configuration apply via cwd.
    if getattr(args, "_workspace_owner", None):
        return await _cmd_test_async(args)
    try:
        root, cfg = _load_config(args)
        from jaunt.workspace import resolve_workspace

        workspace = resolve_workspace(root, cfg)
    except (JauntConfigError, JauntDiscoveryError) as exc:
        _print_error(exc)
        if _is_json_mode(args):
            _emit_json({"command": "test", "ok": False, "error": str(exc)})
        return EXIT_CONFIG_OR_DISCOVERY

    owners = workspace.owner_dirs
    if len(owners) <= 1:
        return await _cmd_test_async(args)

    if not bool(args.no_build):
        build_rc = await _cmd_build_async(args)
        if build_rc != EXIT_OK:
            return build_rc

    import contextlib
    import io

    owner_results: list[dict[str, object]] = []
    exit_codes: list[int] = []
    for owner in owners:
        child = argparse.Namespace(**vars(args))
        child._workspace_owner = str(owner)
        child.no_build = True
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            rc = await _cmd_test_async(child)
        exit_codes.append(rc)
        output = captured.getvalue()
        if _is_json_mode(args):
            payload: dict[str, object] = {}
            for line in reversed(output.splitlines()):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    payload = parsed
                    break
            owner_results.append(
                {
                    "owner": str(owner.relative_to(root)),
                    "ok": rc == EXIT_OK,
                    "result": payload,
                }
            )
        else:
            print(f"== {owner.relative_to(root) or Path('.')} ==")
            if output:
                print(output, end="")

    rc = (
        EXIT_CONFIG_OR_DISCOVERY
        if EXIT_CONFIG_OR_DISCOVERY in exit_codes
        else EXIT_GENERATION_ERROR
        if EXIT_GENERATION_ERROR in exit_codes
        else EXIT_PYTEST_FAILURE
        if EXIT_PYTEST_FAILURE in exit_codes
        else EXIT_OK
    )
    if _is_json_mode(args):
        payload: dict[str, object] = {
            "command": "test",
            "ok": rc == EXIT_OK,
            "exit_code": rc,
            "owners": owner_results,
        }
        runtime = getattr(args, "_mixed_runtime", None)
        if runtime is not None:
            payload["cost"] = runtime.summary("py")
            generated: set[str] = set()
            skipped: set[str] = set()
            generation_failed: dict[str, object] = {}
            for owner_result in owner_results:
                result_payload = owner_result.get("result")
                if not isinstance(result_payload, dict):
                    continue
                result_record = cast("dict[str, object]", result_payload)
                generated.update(str(item) for item in _payload_list(result_record, "generated"))
                skipped.update(str(item) for item in _payload_list(result_record, "skipped"))
                failures = result_record.get("generation_failed", {})
                if isinstance(failures, dict):
                    generation_failed.update({str(key): value for key, value in failures.items()})
            payload["generated"] = sorted(generated)
            payload["skipped"] = sorted(skipped)
            payload["generation_failed"] = generation_failed
        _emit_json(payload)
    return rc


def cmd_test(args: argparse.Namespace) -> int:
    context = _typescript_command_context(args)
    if context is not None:
        root, cfg, mode = context
        if (failure := _target_dispatch_failure(args, mode)) is not None:
            return failure
        if mode == "ts":
            return _cmd_typescript_test_loaded(args, root, cfg)
        if mode == "mixed":
            return _cmd_mixed_test(args, root, cfg)
    return asyncio.run(_cmd_test_workspace_async(args))


def cmd_eval(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    error = "jaunt eval is not supported under the Codex engine (rework pending)."
    _eprint(f"error: {error}")
    if json_mode:
        _emit_json({"command": "eval", "ok": False, "error": error})
    return EXIT_CONFIG_OR_DISCOVERY


def cmd_cache(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
    except (JauntConfigError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "cache", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    from jaunt.cache import ResponseCache

    cache_dir = root / ".jaunt" / "cache"
    rc = ResponseCache(cache_dir)
    subcmd = args.cache_command

    if subcmd == "info":
        info = rc.info()
        if json_mode:
            _emit_json({"command": "cache info", "ok": True, **info})
        else:
            size_mb = int(info["size_bytes"]) / (1024 * 1024)  # type: ignore[arg-type]
            print(f"Cache directory: {info['path']}")
            print(f"Entries: {info['entries']}")
            print(f"Size: {size_mb:.2f} MB")
        return EXIT_OK

    if subcmd == "clear":
        count = rc.clear_all()
        if json_mode:
            _emit_json({"command": "cache clear", "ok": True, "removed": count})
        else:
            print(f"Cleared {count} cache entries.")
        return EXIT_OK

    return EXIT_CONFIG_OR_DISCOVERY


def cmd_watch(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)

    try:
        root, cfg = _load_config(args)
    except (JauntConfigError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "watch", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    from jaunt.watcher import check_watchfiles_available

    try:
        check_watchfiles_available()
    except ImportError as e:
        _eprint(f"error: {e}")
        if json_mode:
            _emit_json({"command": "watch", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    from jaunt.watcher import (
        WatchCycleResult,
        WatchScope,
        build_cycle_runner,
        format_watch_cycle_json,
        make_watchfiles_iter,
        run_watch_loop,
    )

    def expand_directories(entries: Sequence[str]) -> list[Path]:
        expanded: set[Path] = set()
        for entry in entries:
            matches = list(root.glob(entry)) if glob.has_magic(entry) else [root / entry]
            expanded.update(path.resolve() for path in matches if path.is_dir())
        return sorted(expanded)

    def expand_files(entries: Sequence[str]) -> list[Path]:
        expanded: set[Path] = set()
        for entry in entries:
            matches = list(root.glob(entry)) if glob.has_magic(entry) else [root / entry]
            expanded.update(path.resolve() for path in matches if path.is_file())
        return sorted(expanded)

    run_tests = bool(getattr(args, "test", False))
    _, explicit_config_path = _resolve_root_and_config(args)
    watched_config_path = explicit_config_path or root / "jaunt.toml"

    def make_watch_scope(config: JauntConfig) -> WatchScope:
        source_roots: list[Path] = []
        test_roots: list[Path] = []
        if config.version == 1 or config.python_target is not None:
            from jaunt.workspace import resolve_workspace

            workspace = resolve_workspace(root, config)
            source_roots = list(workspace.source_roots)
            test_roots = [route.root for route in workspace.test_roots] if run_tests else []

        ts_source_roots: list[Path] = []
        ts_test_roots: list[Path] = []
        config_paths: list[Path] = [watched_config_path]
        ts_generated_dir = "__generated__"
        if config.typescript_target is not None:
            target = config.typescript_target
            ts_source_roots = expand_directories(target.source_roots)
            # Test specs and fixtures affect generated batteries even when --test
            # is toggled later during a long-lived watch session.
            ts_test_roots = expand_directories(target.test_roots)
            config_paths.extend(expand_files([*target.projects, *target.test_projects]))
            if target.vitest_config:
                config_paths.extend(expand_files([target.vitest_config]))
            config_paths.extend(
                Path(value).resolve()
                for value in (
                    config.typescript_prompts.build_system,
                    config.typescript_prompts.build_module,
                    config.typescript_prompts.test_system,
                    config.typescript_prompts.test_module,
                    config.typescript_prompts.design_system,
                    config.typescript_prompts.design_user,
                )
                if value
            )
            ts_generated_dir = target.generated_dir

        return WatchScope(
            source_roots=tuple(source_roots),
            test_roots=tuple(test_roots),
            generated_dir=config.paths.generated_dir,
            typescript_source_roots=tuple(ts_source_roots),
            typescript_test_roots=tuple(ts_test_roots),
            typescript_generated_dir=ts_generated_dir,
            workspace_root=root,
            config_paths=tuple(sorted(set(config_paths))),
        )

    watch_scope = make_watch_scope(cfg)

    def current_watch_scope() -> WatchScope:
        refreshed_root, refreshed_config = _load_config(args)
        if refreshed_root.resolve() != root.resolve():
            raise JauntConfigError(
                f"Watch root changed from {root.resolve()} to {refreshed_root.resolve()}; "
                "restart `jaunt watch`."
            )
        return make_watch_scope(refreshed_config)

    watch_paths = sorted(
        {
            path
            for path in [
                *watch_scope.source_roots,
                *watch_scope.test_roots,
                *watch_scope.typescript_source_roots,
                *watch_scope.typescript_test_roots,
            ]
            if path.exists()
        }
    )
    # The workspace-root watch is intentional: the filtering scope can change
    # after jaunt.toml is edited, and watchfiles cannot otherwise observe a new
    # sibling root (for example source_roots changing from src to src2).
    watch_paths.append(root)
    for config_path in watch_scope.config_paths:
        if not config_path.resolve().is_relative_to(root.resolve()):
            watch_paths.append(config_path.resolve().parent)
    watch_paths = sorted(set(watch_paths))

    if not watch_paths:
        msg = "No existing source or test roots to watch."
        _eprint(f"error: {msg}")
        if json_mode:
            _emit_json({"command": "watch", "ok": False, "error": msg})
        return EXIT_CONFIG_OR_DISCOVERY

    runner = build_cycle_runner(args, run_tests=run_tests)

    def on_event(msg: str) -> None:
        if not json_mode:
            _eprint(msg)

    def on_cycle_result(result: WatchCycleResult) -> None:
        if json_mode:
            _emit_json(format_watch_cycle_json(result))

    def on_error(e: BaseException) -> None:
        _eprint(f"[watch] error: {e}")

    if not json_mode:
        n = len(watch_paths)
        dirs_word = "directory" if n == 1 else "directories"
        _eprint(f"[watch] watching {n} {dirs_word}... (Ctrl+C to stop)")

    try:
        asyncio.run(
            run_watch_loop(
                changes_iter=make_watchfiles_iter(watch_paths),
                run_cycle=runner,
                on_event=on_event,
                on_cycle_result=on_cycle_result,
                on_error=on_error,
                source_roots=watch_scope.source_roots,
                test_roots=watch_scope.test_roots,
                generated_dir=watch_scope.generated_dir,
                typescript_source_roots=watch_scope.typescript_source_roots,
                typescript_test_roots=watch_scope.typescript_test_roots,
                typescript_generated_dir=watch_scope.typescript_generated_dir,
                workspace_root=watch_scope.workspace_root,
                config_paths=watch_scope.config_paths,
                watch_scope_provider=current_watch_scope,
            )
        )
    except KeyboardInterrupt:
        if not json_mode:
            _eprint("\n[watch] stopped.")

    return EXIT_OK


def _resolve_skill_root(args: argparse.Namespace) -> Path:
    if getattr(args, "root", None):
        return Path(args.root).resolve()
    from jaunt.config import find_project_root

    try:
        return find_project_root(Path.cwd())
    except JauntConfigError:
        return Path.cwd().resolve()


def cmd_skill(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    subcmd = args.skill_command

    from jaunt.skill_manager import (
        add_skill,
        discover_all_skills,
        find_importable_skills,
        import_skills,
        remove_auto_skills,
        remove_skill,
        show_skill,
    )

    if subcmd == "list":
        root = _resolve_skill_root(args)
        skills = discover_all_skills(root)
        if json_mode:
            _emit_json(
                {
                    "command": "skill list",
                    "ok": True,
                    "skills": [
                        {
                            "name": s.name,
                            "source": s.source,
                            "dist": s.dist,
                            "version": s.version,
                            "path": str(s.path),
                        }
                        for s in skills
                    ],
                }
            )
        else:
            if not skills:
                print("No skills found.")
            else:
                for s in skills:
                    tag = f" ({s.source})" if s.source == "auto" else ""
                    print(f"  {s.name}{tag}")
        return EXIT_OK

    if subcmd == "add":
        root = _resolve_skill_root(args)
        lib_refs = None
        if getattr(args, "libs", None):
            from jaunt.lib_inspect import resolve_lib

            try:
                # Resolve relative lib paths against --root, not CWD
                resolved_libs = []
                for lib in args.libs:
                    lib = lib.strip()
                    if not Path(lib).is_absolute() and ("/" in lib or Path(root / lib).is_dir()):
                        lib = str(root / lib)
                    resolved_libs.append(lib)
                lib_refs = [resolve_lib(lib) for lib in resolved_libs]
            except ValueError as e:
                _eprint(f"error: {e}")
                if json_mode:
                    _emit_json({"command": "skill add", "ok": False, "error": str(e)})
                return EXIT_CONFIG_OR_DISCOVERY
        try:
            path = add_skill(
                root, args.name, description=getattr(args, "description", None), libs=lib_refs
            )
        except (FileExistsError, ValueError) as e:
            _eprint(f"error: {e}")
            if json_mode:
                _emit_json({"command": "skill add", "ok": False, "error": str(e)})
            return EXIT_CONFIG_OR_DISCOVERY
        if json_mode:
            _emit_json({"command": "skill add", "ok": True, "path": str(path)})
        else:
            print(f"Created skill: {path}")
        return EXIT_OK

    if subcmd in ("remove", "rm"):
        root = _resolve_skill_root(args)
        if not getattr(args, "force", False):
            # Without -f: show info, do NOT delete
            try:
                content = show_skill(root, args.name)
            except (FileNotFoundError, ValueError) as e:
                _eprint(f"error: {e}")
                if json_mode:
                    _emit_json({"command": "skill remove", "ok": False, "error": str(e)})
                return EXIT_CONFIG_OR_DISCOVERY
            from jaunt.skill_manager import skills_dir

            skill_path = skills_dir(root) / args.name
            if json_mode:
                _emit_json(
                    {
                        "command": "skill remove",
                        "ok": True,
                        "dry_run": True,
                        "name": args.name,
                        "path": str(skill_path),
                    }
                )
            else:
                print(f"Skill '{args.name}' exists at {skill_path}. Rerun with -f to remove.")
            return EXIT_OK
        try:
            path = remove_skill(root, args.name)
        except (FileNotFoundError, ValueError) as e:
            _eprint(f"error: {e}")
            if json_mode:
                _emit_json({"command": "skill remove", "ok": False, "error": str(e)})
            return EXIT_CONFIG_OR_DISCOVERY
        if json_mode:
            _emit_json({"command": "skill remove", "ok": True, "removed": str(path)})
        else:
            print(f"Removed skill: {path}")
        return EXIT_OK

    if subcmd == "show":
        root = _resolve_skill_root(args)
        try:
            content = show_skill(root, args.name)
        except (FileNotFoundError, ValueError) as e:
            _eprint(f"error: {e}")
            return EXIT_CONFIG_OR_DISCOVERY
        print(content, end="")
        return EXIT_OK

    if subcmd == "refresh":
        try:
            root, cfg = _load_config(args)
        except (JauntConfigError, KeyError) as e:
            _print_error(e)
            if json_mode:
                _emit_json({"command": "skill refresh", "ok": False, "error": str(e)})
            return EXIT_CONFIG_OR_DISCOVERY

        _maybe_load_dotenv(root)

        if getattr(args, "force", False):
            removed = remove_auto_skills(root)
            if not json_mode:
                for name in removed:
                    _eprint(f"removed auto-skill: {name}")

        from jaunt.workspace import resolve_workspace

        workspace = resolve_workspace(root, cfg)
        source_dirs = list(workspace.source_roots)
        refresh_ok = True
        refresh_error: str | None = None
        try:
            from jaunt import skills_auto

            res = asyncio.run(
                skills_auto.ensure_pypi_skills(
                    project_root=root,
                    source_roots=[d for d in source_dirs if d.exists()],
                    generated_dir=cfg.paths.generated_dir,
                    llm=cfg.llm,
                    agent=cfg.agent,
                    codex=cfg.codex,
                )
            )
            for w in res.warnings:
                _eprint(f"warn: {w}")
            if res.generation_failures > 0:
                refresh_ok = False
                refresh_error = f"{res.generation_failures} skill(s) failed to generate"
        except Exception as e:  # noqa: BLE001
            refresh_ok = False
            refresh_error = f"{type(e).__name__}: {e}"
            _eprint(f"error: {refresh_error}")

        skills = discover_all_skills(root)
        if json_mode:
            payload: dict[str, object] = {
                "command": "skill refresh",
                "ok": refresh_ok,
                "skills": [s.name for s in skills],
            }
            if refresh_error:
                payload["error"] = refresh_error
            _emit_json(payload)
        else:
            if refresh_ok:
                print(f"Refreshed. {len(skills)} skill(s) on disk.")
            else:
                _eprint(f"Refresh failed: {refresh_error}")
        return EXIT_OK if refresh_ok else EXIT_GENERATION_ERROR

    if subcmd == "import":
        root = _resolve_skill_root(args)
        from_dir = Path(args.from_dir).resolve() if getattr(args, "from_dir", None) else None
        dry_run = bool(getattr(args, "dry_run", False))
        selected_names = list(getattr(args, "names", []) or [])
        import_all = bool(getattr(args, "import_all", False))
        available = find_importable_skills(root, from_dir=from_dir)
        available_names = [name for name, _path in available]
        if import_all and selected_names:
            msg = "Use either explicit skill names or --all, not both."
            _eprint(f"error: {msg}")
            if json_mode:
                _emit_json(
                    {
                        "command": "skill import",
                        "ok": False,
                        "error": msg,
                        "available": available_names,
                    }
                )
            return EXIT_CONFIG_OR_DISCOVERY
        if not import_all and not selected_names:
            msg = "Specify skill names to import, or pass --all."
            _eprint(f"error: {msg}")
            if not json_mode and available_names:
                print("Importable skills:")
                for name in available_names:
                    print(f"  {name}")
            if json_mode:
                _emit_json(
                    {
                        "command": "skill import",
                        "ok": False,
                        "error": msg,
                        "available": available_names,
                    }
                )
            return EXIT_CONFIG_OR_DISCOVERY
        try:
            results = import_skills(
                root,
                names=None if import_all else selected_names,
                from_dir=from_dir,
                dry_run=dry_run,
            )
        except ValueError as e:
            _eprint(f"error: {e}")
            if json_mode:
                _emit_json(
                    {
                        "command": "skill import",
                        "ok": False,
                        "error": str(e),
                        "available": available_names,
                    }
                )
            return EXIT_CONFIG_OR_DISCOVERY
        if json_mode:
            _emit_json(
                {
                    "command": "skill import",
                    "ok": True,
                    "dry_run": dry_run,
                    "selected": sorted(available_names if import_all else selected_names),
                    "results": [{"name": n, "source": str(p), "status": s} for n, p, s in results],
                }
            )
        else:
            if not results:
                print("No importable skills found.")
            else:
                for name, source, status in results:
                    print(f"  {name}: {status} (from {source})")
        return EXIT_OK

    if subcmd == "build":
        from jaunt.skill_manager import _atomic_write_text, read_skill_meta

        root = _resolve_skill_root(args)
        # Verify skill exists
        try:
            existing = show_skill(root, args.name)
        except (FileNotFoundError, ValueError):
            msg = f"Skill not found. Create it with `jaunt skill add {args.name}`"
            _eprint(f"error: {msg}")
            if json_mode:
                _emit_json({"command": "skill build", "ok": False, "error": msg})
            return EXIT_CONFIG_OR_DISCOVERY

        # Read metadata
        meta = read_skill_meta(root, args.name)
        if meta is None or not meta.libs:
            msg = (
                f"No library references found for skill '{args.name}'. "
                f"Recreate it with `jaunt skill add {args.name} --lib <LIB>`."
            )
            _eprint(f"error: {msg}")
            if json_mode:
                _emit_json({"command": "skill build", "ok": False, "error": msg})
            return EXIT_CONFIG_OR_DISCOVERY

        # Load config for LLM settings
        try:
            _root, cfg = _load_config(args)
        except (JauntConfigError, KeyError) as e:
            _print_error(e)
            if json_mode:
                _emit_json({"command": "skill build", "ok": False, "error": str(e)})
            return EXIT_CONFIG_OR_DISCOVERY

        _maybe_load_dotenv(_root)

        # Resolve lib refs from META.json and inspect
        from jaunt.lib_inspect import LibRef, inspect_lib

        lib_contents = []
        for lib_dict in meta.libs:
            lib_type = lib_dict.get("type", "pypi")
            if lib_type not in ("pypi", "path"):
                lib_type = "pypi"
            # Resolve stored relative paths back to absolute
            stored_path = lib_dict.get("path")
            if (
                stored_path is not None
                and lib_type == "path"
                and not Path(stored_path).is_absolute()
            ):
                stored_path = str((root / stored_path).resolve())
            ref = LibRef(
                type=lib_type,  # type: ignore[arg-type]
                name=lib_dict.get("name") or "",
                path=stored_path,
                version=lib_dict.get("version"),
                import_roots=[],
            )
            # Re-resolve import roots at build time
            if ref.type == "pypi":
                from jaunt.lib_inspect import _resolve_pypi_import_roots

                roots = _resolve_pypi_import_roots(ref.name)
                ref = LibRef(
                    type=ref.type,
                    name=ref.name,
                    path=ref.path,
                    version=ref.version,
                    import_roots=roots,
                )
            try:
                lib_contents.append(inspect_lib(ref))
            except Exception as e:  # noqa: BLE001
                _eprint(f"warn: failed inspecting {ref.name}: {e}")

        if not lib_contents:
            msg = "Could not inspect any libraries."
            _eprint(f"error: {msg}")
            if json_mode:
                _emit_json({"command": "skill build", "ok": False, "error": msg})
            return EXIT_CONFIG_OR_DISCOVERY

        # Run LLM
        progress = _make_progress(args, label="skill", total=1, json_mode=json_mode)
        try:
            from jaunt.skill_builder import SkillBuilder

            builder = SkillBuilder(cfg.llm, cfg.agent, codex=cfg.codex)
            if progress is not None:
                progress.phase(args.name, "building")
            updated = asyncio.run(
                builder.build_skill(
                    existing,
                    lib_contents,
                    progress=(
                        (lambda stage, detail: progress.phase(args.name, stage, detail))
                        if progress is not None
                        else None
                    ),
                )
            )
        except Exception as e:  # noqa: BLE001
            if progress is not None:
                progress.advance(args.name, ok=False)
                progress.finish()
            msg = f"{type(e).__name__}: {e}"
            _eprint(f"error: {msg}")
            if json_mode:
                _emit_json({"command": "skill build", "ok": False, "error": msg})
            return EXIT_GENERATION_ERROR

        # Write updated SKILL.md atomically
        from jaunt.skill_manager import skills_dir

        skill_md = skills_dir(root) / args.name / "SKILL.md"
        _atomic_write_text(skill_md, updated + "\n")
        if progress is not None:
            progress.advance(args.name, ok=True)
            progress.finish()

        if json_mode:
            _emit_json({"command": "skill build", "ok": True, "path": str(skill_md)})
        else:
            print(f"Updated skill: {skill_md}")
        return EXIT_OK

    return EXIT_CONFIG_OR_DISCOVERY


def cmd_specs(args: argparse.Namespace) -> int:
    """List discovered @jaunt.magic specs and their dependency graph."""
    context = _typescript_command_context(args)
    if context is not None:
        root, cfg, mode = context
        if (failure := _target_dispatch_failure(args, mode)) is not None:
            return failure
        if mode == "ts":
            return _cmd_typescript_specs_loaded(args, root, cfg)
        if mode == "mixed":
            return _cmd_mixed_specs(args, root, cfg)

    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        from jaunt.workspace import resolve_workspace

        workspace = resolve_workspace(root, cfg)
        source_dirs = list(workspace.source_roots)
        _prepend_sys_path([*source_dirs, root])

        from jaunt import discovery, registry
        from jaunt.deps import build_spec_graph

        modules = [route.module for route in workspace.modules]
        discovery.prepare_import_environment(
            module_names=modules,
            roots=[d for d in source_dirs if d.exists()],
        )
        discovery.import_and_collect(modules, kind="magic")

        specs = dict(registry.get_magic_registry())
        infer_default = bool(cfg.build.infer_deps) and not bool(args.no_infer_deps)
        spec_graph = build_spec_graph(specs, infer_default=infer_default)
        newly = _newly_governed_for_workspace(
            list(specs.values()),
            workspace=workspace,
            generated_dir=cfg.paths.generated_dir,
        )

        module_filter = args.module
        spec_list = []
        for ref, entry in sorted(specs.items()):
            if module_filter and entry.module != module_filter:
                continue
            spec_list.append(
                {
                    "ref": str(ref),
                    "module": entry.module,
                    "qualname": entry.qualname,
                    "source_file": entry.source_file,
                    "origin": entry.origin,
                    "kwargs": entry.decorator_kwargs,
                    "newly_governed": bool(
                        entry.origin == "module" and entry.qualname in newly.get(entry.module, [])
                    ),
                }
            )
        dep_graph = {
            str(ref): sorted(str(d) for d in deps)
            for ref, deps in sorted(spec_graph.items())
            if not module_filter or str(ref).startswith(module_filter + ":")
        }

        if json_mode:
            _emit_json(
                {
                    "command": "specs",
                    "ok": True,
                    "specs": spec_list,
                    "dependency_graph": dep_graph,
                }
            )
        else:
            print(f"specs: {len(spec_list)}")
            for item in spec_list:
                deps = dep_graph.get(str(item["ref"]), [])
                parts = [f"- {item['ref']} ({item['source_file']})"]
                if item["origin"] == "module":
                    parts.append(" [module]")
                if item["newly_governed"]:
                    parts.append(" [newly governed — first build]")
                if item["kwargs"]:
                    parts.append(f" kwargs={item['kwargs']}")
                if deps:
                    parts.append(f"  <- {', '.join(deps)}")
                print("".join(parts))
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "specs", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY


def _guard_configuration(
    payload: dict[str, object],
) -> tuple[Path | None, JauntConfig | None]:
    try:
        from jaunt.config import find_project_root, load_config

        cwd = payload.get("cwd")
        start = Path(str(cwd)) if cwd else Path.cwd()
        root = find_project_root(start)
        return root, load_config(root=root)
    except Exception:  # noqa: BLE001 - hooks must never break the harness
        return None, None


def _guard_generated_dirs(
    args: argparse.Namespace,
    cfg: JauntConfig | None,
) -> tuple[str, ...]:
    if args.generated_dir:
        return (str(args.generated_dir),)
    if cfg is None:
        return ("__generated__",)
    candidates = [cfg.paths.generated_dir]
    if cfg.typescript_target is not None:
        candidates.append(cfg.typescript_target.generated_dir)
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _guard_generated_dir(args: argparse.Namespace, payload: dict[str, object]) -> str:
    """Backward-compatible single-dir view used by older callers/tests."""

    _root, cfg = _guard_configuration(payload)
    return _guard_generated_dirs(args, cfg)[0]


def _guard_payload_path(payload: dict[str, object]) -> str | None:
    try:
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        tool_input = cast("dict[str, object]", tool_input)
        for key in ("file_path", "path", "notebook_path"):
            value = tool_input.get(key)
            if value:
                return str(value)
    except (AttributeError, TypeError):
        return None
    return None


def _typescript_spec_hint(
    payload: dict[str, object],
    *,
    root: Path,
    generated_dir: str,
) -> str | None:
    """Map a generated TS implementation, mirror, or sidecar back to its spec."""

    raw = _guard_payload_path(payload)
    if raw is None:
        return None
    try:
        cwd = Path(str(payload.get("cwd") or root)).resolve()
        target = Path(raw)
        target = target.resolve() if target.is_absolute() else (cwd / target).resolve()
        relative = target.relative_to(root.resolve())
    except (OSError, ValueError):
        return None

    generated_parts = Path(generated_dir).parts
    if not generated_parts:
        return None
    parts = relative.parts
    index = next(
        (
            offset
            for offset in range(len(parts) - len(generated_parts) + 1)
            if parts[offset : offset + len(generated_parts)] == generated_parts
        ),
        None,
    )
    if index is None or index + len(generated_parts) >= len(parts):
        return None

    artifact = Path(*parts[index + len(generated_parts) :])
    name = artifact.name
    source_exts: tuple[str, ...]
    if name.endswith(".api.tsx"):
        stem, source_exts = name[: -len(".api.tsx")], (".tsx", ".ts")
    elif name.endswith(".api.ts"):
        stem, source_exts = name[: -len(".api.ts")], (".ts", ".tsx")
    elif name.endswith(".jaunt.json"):
        stem, source_exts = name[: -len(".jaunt.json")], (".ts", ".tsx")
    elif name.endswith(".tsx"):
        stem, source_exts = name[: -len(".tsx")], (".tsx", ".ts")
    elif name.endswith(".ts"):
        stem, source_exts = name[: -len(".ts")], (".ts", ".tsx")
    else:
        return None

    owner = Path(*parts[:index], *artifact.parts[:-1])
    candidates = [owner / f"{stem}.jaunt{extension}" for extension in source_exts]
    existing = next((candidate for candidate in candidates if (root / candidate).is_file()), None)
    return (existing or candidates[0]).as_posix()


def _guard_with_typescript_hint(
    output: dict,
    payload: dict[str, object],
    *,
    root: Path | None,
    cfg: JauntConfig | None,
    generated_dir: str,
) -> dict:
    if root is None or cfg is None or cfg.typescript_target is None:
        return output
    if generated_dir != cfg.typescript_target.generated_dir:
        return output
    hint = _typescript_spec_hint(payload, root=root, generated_dir=generated_dir)
    if hint is None:
        return output
    try:
        decision = output["hookSpecificOutput"]
        path = _guard_payload_path(payload)
        if not isinstance(decision, dict) or path is None:
            return output
        decision["permissionDecisionReason"] = (
            f"{path} is machine-owned generated TypeScript (jaunt). Edit the spec instead: "
            f"{hint}. Changes here are overwritten on the next build."
        )
    except (KeyError, TypeError):
        return output
    return output


def cmd_guard(args: argparse.Namespace) -> int:
    from jaunt import guard as guard_mod

    try:
        payload_obj = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - hooks must never break the harness
        return EXIT_OK
    payload = cast("dict[str, object]", payload_obj) if isinstance(payload_obj, dict) else {}
    root, cfg = _guard_configuration(payload)
    for generated_dir in _guard_generated_dirs(args, cfg):
        out = guard_mod.evaluate(payload, generated_dir=generated_dir)
        if out is None:
            continue
        print(
            json.dumps(
                _guard_with_typescript_hint(
                    out,
                    payload,
                    root=root,
                    cfg=cfg,
                    generated_dir=generated_dir,
                )
            )
        )
        break
    return EXIT_OK


def cmd_install_claude_plugin(args: argparse.Namespace) -> int:
    from jaunt import claude_plugin

    json_mode = _is_json_mode(args)
    local = bool(getattr(args, "local", False))

    def _fail(msg: str, code: int) -> int:
        _eprint(f"error: {msg}")
        if json_mode:
            _emit_json({"command": "install-claude-plugin", "ok": False, "error": msg})
        return code

    if shutil.which("claude") is None:
        return _fail(claude_plugin.missing_cli_message(), EXIT_CONFIG_OR_DISCOVERY)

    local_path: str | None = None
    if local:
        root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
        manifest = root / ".claude-plugin" / "marketplace.json"
        if not manifest.is_file():
            return _fail(
                f"No .claude-plugin/marketplace.json under {root}. Run from a Jaunt "
                "clone's repo root, or drop --local to install from GitHub.",
                EXIT_CONFIG_OR_DISCOVERY,
            )
        local_path = str(root)

    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )

    def _step(argv: list[str], *, ok_label: str) -> tuple[str | None, str | None]:
        """Run one claude command; return (status_label, error_message).

        ``status_label`` is ``ok_label`` on a clean run or ``"already"`` for an
        idempotent no-op; on failure it is ``None`` and the message is set.
        """
        try:
            proc = _run(argv)
        except (subprocess.TimeoutExpired, OSError) as e:
            return None, str(e)
        status = claude_plugin.classify_result(proc.returncode, proc.stdout, proc.stderr)
        if status == "error":
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            return None, detail
        return (ok_label if status == "ok" else "already"), None

    market_status, err = _step(
        claude_plugin.marketplace_add_command(local_path=local_path), ok_label="added"
    )
    if err is not None:
        return _fail(err, 1)

    plugin_status, err = _step(claude_plugin.plugin_install_command(), ok_label="installed")
    if err is not None:
        return _fail(err, 1)

    if json_mode:
        _emit_json(
            {
                "command": "install-claude-plugin",
                "ok": True,
                "marketplace": market_status,
                "plugin": plugin_status,
                "local": local,
            }
        )
    else:
        market_line = "added" if market_status == "added" else "already present"
        plugin_line = "installed" if plugin_status == "installed" else "already installed"
        print(f"Marketplace jaunt-plugins: {market_line}.")
        print(f"Plugin jaunt: {plugin_line}.")
        print(f"See {claude_plugin.DOCS_URL} for what the plugin adds.")
    return EXIT_OK


def cmd_install_codex_plugin(args: argparse.Namespace) -> int:
    from jaunt import codex_plugin

    json_mode = _is_json_mode(args)
    local = bool(getattr(args, "local", False))

    def _fail(msg: str, code: int) -> int:
        _eprint(f"error: {msg}")
        if json_mode:
            _emit_json({"command": "install-codex-plugin", "ok": False, "error": msg})
        return code

    if shutil.which("codex") is None:
        return _fail(codex_plugin.missing_cli_message(), EXIT_CONFIG_OR_DISCOVERY)

    local_path: str | None = None
    if local:
        root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
        manifest = root / ".agents" / "plugins" / "marketplace.json"
        if not manifest.is_file():
            return _fail(
                f"No .agents/plugins/marketplace.json under {root}. Run from a Jaunt "
                "clone's repo root, or drop --local to install from GitHub.",
                EXIT_CONFIG_OR_DISCOVERY,
            )
        local_path = str(root)

    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )

    def _step(argv: list[str], *, ok_label: str) -> tuple[str | None, str | None]:
        try:
            proc = _run(argv)
        except (subprocess.TimeoutExpired, OSError) as e:
            return None, str(e)
        status = codex_plugin.classify_result(proc.returncode, proc.stdout, proc.stderr)
        if status == "error":
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            return None, detail
        return (ok_label if status == "ok" else "already"), None

    market_status, err = _step(
        codex_plugin.marketplace_add_command(local_path=local_path), ok_label="added"
    )
    if err is not None:
        return _fail(err, 1)

    plugin_status, err = _step(codex_plugin.plugin_install_command(), ok_label="installed")
    if err is not None:
        return _fail(err, 1)

    if json_mode:
        _emit_json(
            {
                "command": "install-codex-plugin",
                "ok": True,
                "marketplace": market_status,
                "plugin": plugin_status,
                "local": local,
            }
        )
    else:
        market_line = "added" if market_status == "added" else "already present"
        plugin_line = "installed" if plugin_status == "installed" else "already installed"
        print(f"Marketplace {codex_plugin.MARKETPLACE_NAME}: {market_line}.")
        print(f"Plugin jaunt: {plugin_line}.")
        print("Start a new Codex session, then review the bundled hooks with /hooks.")
        print(f"See {codex_plugin.DOCS_URL} for what the plugin adds.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as e:
        # argparse uses SystemExit for --help/--version and parse errors.
        code = e.code
        return int(code) if isinstance(code, int) else EXIT_CONFIG_OR_DISCOVERY

    if args.command == "build":
        return cmd_build(args)
    if args.command == "test":
        return cmd_test(args)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "clean":
        return cmd_clean(args)
    if args.command == "migrate":
        return cmd_migrate(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "sync":
        return cmd_sync(args)
    if args.command == "design":
        return cmd_design(args)
    if args.command == "log":
        return cmd_log(args)
    if args.command == "daemon":
        return cmd_daemon(args)
    if args.command == "jobs":
        return cmd_jobs(args)
    if args.command == "guard":
        return cmd_guard(args)
    if args.command == "install-claude-plugin":
        return cmd_install_claude_plugin(args)
    if args.command == "install-codex-plugin":
        return cmd_install_codex_plugin(args)
    if args.command == "instructions":
        return cmd_instructions(args)
    if args.command == "tree":
        return cmd_tree(args)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "reconcile":
        return cmd_reconcile(args)
    if args.command == "adopt":
        return cmd_adopt(args)
    if args.command == "eject":
        return cmd_eject(args)
    if args.command == "eval":
        return cmd_eval(args)
    if args.command == "watch":
        return cmd_watch(args)
    if args.command == "specs":
        return cmd_specs(args)
    if args.command == "cache":
        return cmd_cache(args)
    if args.command in ("skill", "skills"):
        return cmd_skill(args)

    return EXIT_CONFIG_OR_DISCOVERY


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
