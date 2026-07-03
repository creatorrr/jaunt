"""CLI entry point for Jaunt.

Think about where you want to be, and you're there -- that's jaunting.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
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
    from jaunt.jobs import JobRecord
    from jaunt.registry import SpecEntry
    from jaunt.spec_ref import SpecRef


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

    test_p = subparsers.add_parser("test", help="Generate tests and run pytest.")
    _add_common_flags(test_p)
    _add_build_generation_flags(test_p)
    test_p.add_argument("--no-build", action="store_true", help="Skip `jaunt build`.")
    test_p.add_argument("--no-run", action="store_true", help="Skip running pytest.")
    test_p.add_argument(
        "--pytest-args",
        action="append",
        default=[],
        help="Extra args appended to pytest (repeatable).",
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

    reconcile_p = subparsers.add_parser(
        "reconcile", help="Derive/refresh committed contract batteries (calls the model)."
    )
    _add_common_flags(reconcile_p)

    adopt_p = subparsers.add_parser("adopt", help="Add @jaunt.contract to a function and derive.")
    adopt_p.add_argument("ref", help="Spec ref 'module:func'.")
    _add_common_flags(adopt_p)

    eject_p = subparsers.add_parser("eject", help="Remove contract tracking; leave plain pytest.")
    eject_p.add_argument("ref", nargs="?", default=None, help="Spec ref 'module:func'.")
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

    test_dirs = [root / tr for tr in cfg.paths.test_roots]
    existing_test_dirs = [d for d in test_dirs if d.exists()]
    modules_set: set[str] = set()
    for tr, test_dir in zip(cfg.paths.test_roots, test_dirs, strict=False):
        if not test_dir.exists():
            continue
        prefix = ".".join(Path(tr).parts)
        mods = discovery.discover_modules(
            roots=[test_dir],
            exclude=[],
            generated_dir=cfg.paths.generated_dir,
            module_prefix=prefix or None,
        )
        modules_set.update(mods)
    return existing_test_dirs, sorted(modules_set)


def _discover_contract_specs(*, root: Path, cfg: JauntConfig) -> dict[SpecRef, SpecEntry]:
    from jaunt import discovery, registry

    source_dirs = [root / sr for sr in cfg.paths.source_roots]
    _prepend_sys_path([*source_dirs, root])
    registry.clear_registries()
    modules = discovery.discover_modules(
        roots=[d for d in source_dirs if d.exists()],
        exclude=[],
        generated_dir=cfg.paths.generated_dir,
    )
    discovery.evict_modules_for_import(
        module_names=modules, roots=[d for d in source_dirs if d.exists()]
    )
    discovery.import_and_collect(modules, kind="contract")
    return dict(registry.get_contract_registry())


def _resolve_contract_source_file(*, root: Path, cfg: JauntConfig, module: str) -> Path:
    from jaunt import discovery

    source_dirs = [root / sr for sr in cfg.paths.source_roots if (root / sr).exists()]
    found = discovery.discover_module_files(
        roots=source_dirs,
        exclude=[],
        generated_dir=cfg.paths.generated_dir,
        target_modules={module},
    )
    for mod, path in found:
        if mod == module:
            return path
    raise JauntDiscoveryError(f"Could not locate source module {module!r} under source_roots.")


def _build_backend(cfg: JauntConfig):
    from jaunt.generate.codex_backend import CodexBackend

    return CodexBackend(cfg.codex, cfg.llm, cfg.prompts)


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
            for job in records[-10:]:
                print(f"- {job.id} {job.module}: {_job_state_label(job)}")
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


def _jobs_would_rebuild(root: Path, args: argparse.Namespace) -> dict[str, str]:
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
    return f"[wait] {job.id} {job.module}: {status}"


def _wait_payload(job: JobRecord) -> dict[str, str]:
    return {
        "id": job.id,
        "module": job.module,
        "state": job.state,
        "phase": job.phase,
        "error": job.error,
    }


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
        print(f"- {job.id} {job.module}: {_job_state_label(job)}")
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
        else:
            print(f"{key}: {value}")
    if args.full and job.detail_log:
        detail = Path(job.detail_log)
        if detail.exists():
            print(detail.read_text(encoding="utf-8"), end="")
    return EXIT_OK


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
        current_digest = _module_current_digest(root, args, job.module)
        if current_digest is None or current_digest != job.spec_digest:
            _eprint(
                f"error: {job.module} spec changed since this job parked; "
                "the daemon will rebuild it -- use --force to land anyway"
            )
            return EXIT_PYTEST_FAILURE

    try:
        expected_head = landing.git_out(root, "rev-parse", "HEAD").strip()
        sha = landing.land(
            root,
            patch,
            patch_paths=patch_paths_raw,
            message=landing.build_commit_message(
                job.module,
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


def cmd_jobs(args: argparse.Namespace) -> int:
    if args.jobs_command == "show":
        return _cmd_jobs_show(args)
    if args.jobs_command == "retry":
        return _cmd_jobs_retry(args)
    if args.jobs_command == "wait":
        return _cmd_jobs_wait(args)
    return _cmd_jobs_list(args)


def _sync_generated_dir_env(cfg: JauntConfig) -> None:
    """Propagate generated_dir to env so runtime forwarding uses the right path."""
    os.environ["JAUNT_GENERATED_DIR"] = cfg.paths.generated_dir


def _maybe_load_dotenv(root: Path) -> None:
    # Best-effort; never override existing environment variables.
    load_dotenv_into_environ(root / ".env")


_INIT_TEMPLATE = """\
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


_INIT_SPEC_TEMPLATE = '''\
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

    # Ensure default directories exist.
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)

    toml_path.write_text(_INIT_TEMPLATE, encoding="utf-8")
    spec_path = root / "src" / "specs.py"
    spec_created = False
    if not spec_path.exists():
        spec_path.write_text(_INIT_SPEC_TEMPLATE, encoding="utf-8")
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

    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
    except (JauntConfigError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "clean", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    generated_dir = cfg.paths.generated_dir
    scan_roots = [root / sr for sr in cfg.paths.source_roots] + [
        root / tr for tr in cfg.paths.test_roots
    ]
    found = _find_generated_dirs(scan_roots, generated_dir)
    dry_run = getattr(args, "dry_run", False)

    if dry_run:
        if json_mode:
            _emit_json(
                {
                    "command": "clean",
                    "ok": True,
                    "dry_run": True,
                    "would_remove": [str(p) for p in found],
                }
            )
        return EXIT_OK

    for d in found:
        shutil.rmtree(d)

    if json_mode:
        _emit_json(
            {
                "command": "clean",
                "ok": True,
                "removed": [str(p) for p in found],
            }
        )

    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        from jaunt.contract import runner
        from jaunt.contract.drift import BLOCKING_MESSAGE, is_blocking

        specs = _discover_contract_specs(root=root, cfg=cfg)
        if not specs:
            if json_mode:
                _emit_json({"command": "check", "ok": True, "blocked": [], "checked": []})
            else:
                print("Contract check: 0 contract function(s).")
            return EXIT_OK

        def _run(path: Path) -> bool:
            return runner.run_battery_file(path, root=root, source_roots=cfg.paths.source_roots)

        results = [
            runner.evaluate_entry(
                root,
                cfg.contract.battery_dir,
                cfg.contract.derive,
                entry,
                run_battery=_run,
            )
            for entry in sorted(specs.values(), key=lambda e: str(e.spec_ref))
        ]
        blocked = [r for r in results if is_blocking(r.state)]

        if json_mode:
            _emit_json(
                {
                    "command": "check",
                    "ok": not blocked,
                    "blocked": [{"ref": r.spec_ref, "state": r.state.value} for r in blocked],
                    "checked": [{"ref": r.spec_ref, "state": r.state.value} for r in results],
                }
            )
        else:
            for r in results:
                mark = "BLOCK" if is_blocking(r.state) else "ok"
                line = f"[{mark}] {r.spec_ref}: {r.state.value}"
                if is_blocking(r.state):
                    line += f" — {BLOCKING_MESSAGE.get(r.state, '')}"
                print(line)
            print(f"Contract check: {len(results)} checked, {len(blocked)} blocked.")

        return EXIT_PYTEST_FAILURE if blocked else EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "check", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY


def cmd_reconcile(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        import importlib

        from jaunt import __version__

        root, cfg = _load_config(args)
        from jaunt.contract import runner
        from jaunt.contract.derive import extract_blocks_via_model
        from jaunt.generate.base import GeneratorBackend

        _backend_box: list[GeneratorBackend] = []

        def _model_extract(prose: str):
            if not _backend_box:
                _backend_box.append(_build_backend(cfg))
            backend = _backend_box[0]

            async def _complete(system: str, user: str) -> str:
                return await backend.complete_text(system=system, user=user)

            return asyncio.run(extract_blocks_via_model(prose, complete=_complete))

        specs = _discover_contract_specs(root=root, cfg=cfg)
        target_mods = _iter_target_modules(getattr(args, "target", []) or [])

        results = []
        for entry in sorted(specs.values(), key=lambda e: str(e.spec_ref)):
            if target_mods and entry.module not in target_mods:
                continue
            module = importlib.import_module(entry.module)
            results.append(
                runner.reconcile_entry(
                    root,
                    cfg.contract.battery_dir,
                    cfg.contract.derive,
                    cfg.contract.strength,
                    entry,
                    module_namespace=vars(module),
                    tool_version=__version__,
                    model_extract=_model_extract,
                    source_roots=cfg.paths.source_roots,
                )
            )

        failed = [r for r in results if not r.ok]
        if json_mode:
            _emit_json(
                {
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
            )
        else:
            for r in results:
                if r.ok:
                    excluded = r.strength_excluded
                    suffix = f" ({excluded} fixture cases not scored)" if excluded else ""
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


def cmd_adopt(args: argparse.Namespace) -> int:
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
        result = runner.reconcile_entry(
            root,
            cfg.contract.battery_dir,
            cfg.contract.derive,
            cfg.contract.strength,
            entry,
            module_namespace=vars(mod),
            tool_version=__version__,
            source_roots=cfg.paths.source_roots,
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
            suffix = f" ({excluded} fixture cases not scored)" if excluded else ""
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
            path = runner.battery_path(root, cfg.contract.battery_dir, entry)
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
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        magic_only = bool(getattr(args, "magic_only", False))
        include_target_tests = _effective_include_target_tests(cfg, args)
        build_instructions = _effective_build_instructions(cfg, args)

        source_dirs = [root / sr for sr in cfg.paths.source_roots]
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

            def _run_battery(path: Path) -> bool:
                return contract_runner.run_battery_file(
                    path, root=root, source_roots=cfg.paths.source_roots
                )

            statuses = {
                str(e.spec_ref): contract_runner.evaluate_entry(
                    root,
                    cfg.contract.battery_dir,
                    cfg.contract.derive,
                    e,
                    run_battery=_run_battery,
                )
                for e in contract_specs.values()
            }

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

        if mstatus.total == 0:
            if json_mode:
                payload: dict[str, object] = {
                    "command": "status",
                    "ok": True,
                    "stale": [],
                    "stale_changes": {},
                    "fresh": [],
                    "digests": mstatus.digests,
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
                        suffix = f" ({excluded} fixture cases not scored)" if excluded else ""
                        print(
                            f"- {row['ref']}: {row['state']} (strength {row['strength']}){suffix}"
                            + flag
                        )
                if tree_drift is not None:
                    print(f"tree: {tree_drift} (run jaunt tree)")
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
                print(f"- {mod} ({stale_changes.get(mod, 'structural')})")
            print(f"Fresh ({len(fresh_sorted)}):")
            for mod in fresh_sorted:
                print(f"- {mod}")
            if contract_rows:
                print(f"Contracts ({len(contract_rows)}):")
                for row in contract_rows:
                    flag = " [review]" if row["review"] else ""
                    excluded = row["strength_excluded"]
                    suffix = f" ({excluded} fixture cases not scored)" if excluded else ""
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


async def _cmd_build_async(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        _maybe_load_dotenv(root)
        _sync_generated_dir_env(cfg)
        include_target_tests = _effective_include_target_tests(cfg, args)
        build_instructions = _effective_build_instructions(cfg, args)

        source_dirs = [root / sr for sr in cfg.paths.source_roots]

        builtin_on = bool(cfg.skills.builtin) and not bool(
            getattr(args, "no_builtin_skills", False)
        )
        builtin_skill_names = tuple(cfg.skills.builtin_skills) if builtin_on else ()
        auto_skills_on = bool(cfg.skills.auto) and not bool(getattr(args, "no_auto_skills", False))
        if auto_skills_on:
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

            repo_map_block = rc_api.repo_map_block_for_build(root=root, cfg=cfg, today=_today())

        from jaunt.skill_seed import skills_fingerprint

        build_skills_digest = skills_fingerprint(
            project_root=root, builtin_names=builtin_skill_names
        )

        _prepend_sys_path([*source_dirs, root])

        from jaunt import discovery, registry
        from jaunt.deps import build_spec_graph, collapse_to_module_dag, find_cycles

        registry.clear_registries()
        modules = discovery.discover_modules(
            roots=[d for d in source_dirs if d.exists()],
            exclude=[],
            generated_dir=cfg.paths.generated_dir,
        )
        discovery.evict_modules_for_import(
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

        from jaunt.cost import CostTracker

        # Created up front so a (best-effort) project-overview model call is charged
        # against the same budget/summary as the per-module build calls below.
        cost_tracker = CostTracker(max_cost=cfg.llm.max_cost_per_build)

        overview_block = ""
        if cfg.context.overview:
            from jaunt.repo_context import overview as rc_overview

            overview_block = await rc_overview.project_overview_block_for_build(
                root=root,
                cfg=cfg,
                module_specs=module_specs,
                repo_map_block=repo_map_block,
                backend=_build_backend(cfg),
                cost_tracker=cost_tracker,
            )
            # Abort early if generating the overview already blew the budget.
            cost_tracker.check_budget()

        package_dir = next((d for d in source_dirs if d.exists()), None)
        if package_dir is None:
            raise JauntConfigError("No existing source_roots to build into.")

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
            expected, _errs = builder._build_expected_names(entries)
            wcc = builder._whole_class_context(
                entries,
                specs=specs,
                package_dir=package_dir,
                generated_dir=cfg.paths.generated_dir,
            )
            build_module_context_digests[module_name] = builder.build_module_context_artifacts(
                module_name=module_name,
                entries=entries,
                expected_names=expected,
                module_specs=module_specs,
                module_dag=module_dag,
                package_dir=package_dir,
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
            force=bool(args.force),
        )
        api_changed = builder.detect_api_changed_modules(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            module_api_digests=build_module_api_digests,
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
                    "tool_version": "",
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
                        package_dir, cfg.paths.generated_dir, module_name
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
            )
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

            rc_search.ensure_index(package_dir)

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
            backend=_build_backend(cfg),
            generation_fingerprint=build_generation_fingerprint,
            repo_map_block=repo_map_block,
            project_overview_block=overview_block,
            search_enabled=search_enabled,
            search_max_hits=cfg.context.search.max_hits,
            source_roots=[d for d in source_dirs if d.exists()],
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
        )

        if report.failed and not json_mode:
            _eprint(format_build_failures(report.failed))

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
        _journal.append_events(root, events)

        if not json_mode and (cost_tracker.api_calls > 0 or cost_tracker.cache_hits > 0):
            _eprint(cost_tracker.format_summary())

        if not json_mode:
            summary = f"Built {len(report.generated)} module(s), skipped {len(report.skipped)}"
            if report.failed:
                summary += f", {len(report.failed)} failed"
            print(f"{summary}.")

        if json_mode:
            _emit_json(
                {
                    "command": "build",
                    "ok": not report.failed,
                    "generated": sorted(report.generated),
                    "skipped": sorted(report.skipped),
                    "refrozen": sorted(refrozen_modules),
                    "failed": {k: v for k, v in sorted(report.failed.items())},
                    "cost": cost_tracker.summary_dict(),
                    "cache": {"hits": response_cache.hits, "misses": response_cache.misses},
                }
            )

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

        source_dirs = [root / sr for sr in cfg.paths.source_roots]
        test_dirs = [root / tr for tr in cfg.paths.test_roots]
        # Import source specs and namespace-package test modules without
        # prepending raw test roots, which can shadow stdlib/dependency imports.
        _prepend_sys_path([*source_dirs, root])

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
            registry.clear_registries()
            src_mods = discovery.discover_modules(
                roots=[d for d in source_dirs if d.exists()],
                exclude=[],
                generated_dir=cfg.paths.generated_dir,
            )
            discovery.evict_modules_for_import(
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
                generated_path = package_dir / relpath
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

        registry.clear_registries()
        modules_set: set[str] = set()
        existing_test_dirs = [d for d in test_dirs if d.exists()]
        first_test_root = Path(cfg.paths.test_roots[0]) if cfg.paths.test_roots else Path("tests")
        tests_package = ".".join(first_test_root.parts) or "tests"
        for tr, test_dir in zip(cfg.paths.test_roots, test_dirs, strict=False):
            if not test_dir.exists():
                continue
            prefix = ".".join(Path(tr).parts)
            mods = discovery.discover_modules(
                roots=[test_dir],
                exclude=[],
                generated_dir=cfg.paths.generated_dir,
                module_prefix=prefix or None,
            )
            modules_set.update(mods)
        modules = sorted(modules_set)
        discovery.evict_modules_for_import(module_names=modules, roots=existing_test_dirs)
        discovery.import_and_collect(modules, kind="test")

        specs = dict(registry.get_test_registry())
        auto_entries = synthesize_auto_class_test_entries(
            build_magic_specs,
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
            project_dir=root,
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
                test_header_fields_by_module[module_name] = {
                    "tool_version": "",
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
                    "module_context_digest": test_module_context_digests.get(module_name, ""),
                    "spec_refs": [str(e.spec_ref) for e in entries],
                }
            test_plan = await tester.plan_test_refreeze_or_rebuild(
                project_dir=root,
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
            )
            test_refrozen_modules = set(test_plan.refrozen)
            stale = set(test_plan.rebuild)

        total = len(stale & set(module_specs.keys()))
        progress = _make_progress(args, label="test", total=total, json_mode=json_mode)

        from jaunt.cache import ResponseCache
        from jaunt.cost import CostTracker

        cache_dir = root / ".jaunt" / "cache"
        no_cache = bool(getattr(args, "no_cache", False))
        response_cache = ResponseCache(cache_dir, enabled=not no_cache)
        cost_tracker = CostTracker(max_cost=cfg.llm.max_cost_per_build)
        backend = _build_backend(cfg)

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
            jobs=int(cfg.build.jobs),
            async_runner=cfg.build.async_runner,
            build_instructions=build_instructions,
            check_generated_imports=cfg.build.check_generated_imports,
            generated_import_allowlist=cfg.build.generated_import_allowlist,
        )

        result = tester.run_tests(
            project_dir=root,
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
            cwd=root,
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

        if json_mode:
            _emit_json(
                {
                    "command": "test",
                    "ok": exit_code == 0 and not gen_failed,
                    "exit_code": exit_code,
                    "refrozen": sorted(test_refrozen_modules),
                    "generation_failed": {k: v for k, v in sorted(gen_failed.items())},
                }
            )

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


def cmd_test(args: argparse.Namespace) -> int:
    return asyncio.run(_cmd_test_async(args))


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

    from jaunt.watcher import check_watchfiles_available

    try:
        check_watchfiles_available()
    except ImportError as e:
        _eprint(f"error: {e}")
        if json_mode:
            _emit_json({"command": "watch", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    try:
        root, cfg = _load_config(args)
    except (JauntConfigError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "watch", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY

    from jaunt.watcher import (
        WatchCycleResult,
        build_cycle_runner,
        format_watch_cycle_json,
        make_watchfiles_iter,
        run_watch_loop,
    )

    source_roots = [root / sr for sr in cfg.paths.source_roots]
    test_roots = [root / tr for tr in cfg.paths.test_roots] if getattr(args, "test", False) else []
    watch_paths = [d for d in (source_roots + test_roots) if d.exists()]

    if not watch_paths:
        msg = "No existing source or test roots to watch."
        _eprint(f"error: {msg}")
        if json_mode:
            _emit_json({"command": "watch", "ok": False, "error": msg})
        return EXIT_CONFIG_OR_DISCOVERY

    run_tests = bool(getattr(args, "test", False))
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
                source_roots=source_roots,
                test_roots=test_roots,
                generated_dir=cfg.paths.generated_dir,
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

        source_dirs = [root / sr for sr in cfg.paths.source_roots]
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
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        source_dirs = [root / sr for sr in cfg.paths.source_roots]
        _prepend_sys_path([*source_dirs, root])

        from jaunt import discovery, registry
        from jaunt.deps import build_spec_graph

        registry.clear_registries()
        modules = discovery.discover_modules(
            roots=[d for d in source_dirs if d.exists()],
            exclude=[],
            generated_dir=cfg.paths.generated_dir,
        )
        discovery.evict_modules_for_import(
            module_names=modules,
            roots=[d for d in source_dirs if d.exists()],
        )
        discovery.import_and_collect(modules, kind="magic")

        specs = dict(registry.get_magic_registry())
        infer_default = bool(cfg.build.infer_deps) and not bool(args.no_infer_deps)
        spec_graph = build_spec_graph(specs, infer_default=infer_default)

        module_filter = args.module
        spec_list = [
            {
                "ref": str(ref),
                "module": entry.module,
                "qualname": entry.qualname,
                "source_file": entry.source_file,
            }
            for ref, entry in sorted(specs.items())
            if not module_filter or entry.module == module_filter
        ]
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
                suffix = f"  <- {', '.join(deps)}" if deps else ""
                print(f"- {item['ref']} ({item['source_file']}){suffix}")
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "specs", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY


def _guard_generated_dir(args: argparse.Namespace, payload: dict[str, object]) -> str:
    if args.generated_dir:
        return str(args.generated_dir)
    try:
        from jaunt.config import find_project_root, load_config

        cwd = payload.get("cwd")
        start = Path(str(cwd)) if cwd else Path.cwd()
        root = find_project_root(start)
        cfg = load_config(root=root)
        return cfg.paths.generated_dir
    except Exception:  # noqa: BLE001 - hooks must never break the harness
        return "__generated__"


def cmd_guard(args: argparse.Namespace) -> int:
    from jaunt import guard as guard_mod

    try:
        payload_obj = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - hooks must never break the harness
        return EXIT_OK
    payload = cast("dict[str, object]", payload_obj) if isinstance(payload_obj, dict) else {}
    out = guard_mod.evaluate(payload, generated_dir=_guard_generated_dir(args, payload))
    if out is not None:
        print(json.dumps(out))
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
    if args.command == "status":
        return cmd_status(args)
    if args.command == "log":
        return cmd_log(args)
    if args.command == "daemon":
        return cmd_daemon(args)
    if args.command == "jobs":
        return cmd_jobs(args)
    if args.command == "guard":
        return cmd_guard(args)
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
