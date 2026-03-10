"""CLI entry point for Jaunt.

Think about where you want to be, and you're there -- that's jaunting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:  # pragma: no cover
    from jaunt.config import JauntConfig
    from jaunt.registry import SpecEntry


EXIT_OK = 0
EXIT_CONFIG_OR_DISCOVERY = 2
EXIT_GENERATION_ERROR = 3
EXIT_PYTEST_FAILURE = 4


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jaunt")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    build_p = subparsers.add_parser("build", help="Generate code for magic specs.")
    _add_common_flags(build_p)

    test_p = subparsers.add_parser("test", help="Generate tests and run pytest.")
    _add_common_flags(test_p)
    test_p.add_argument("--no-build", action="store_true", help="Skip `jaunt build`.")
    test_p.add_argument("--no-run", action="store_true", help="Skip running pytest.")
    test_p.add_argument(
        "--pytest-args",
        action="append",
        default=[],
        help="Extra args appended to pytest (repeatable).",
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
    watch_p.add_argument(
        "--test",
        action="store_true",
        dest="test",
        help="Run tests after each successful build.",
    )

    mcp_p = subparsers.add_parser("mcp", help="MCP server commands.")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command", required=True)
    serve_p = mcp_sub.add_parser("serve", help="Start MCP server (stdio transport).")
    serve_p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root (defaults to searching upward for jaunt.toml).",
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
    skill_import_p.add_argument("--root", type=str, default=None)
    skill_import_p.add_argument(
        "--from", type=str, default=None, dest="from_dir", help="Import from specific directory."
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


def _iter_target_modules(targets: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for t in targets:
        mod = (t or "").split(":", 1)[0].strip()
        if mod:
            out.add(mod)
    return out


def _deps_closure(modules: set[str], *, module_dag: dict[str, set[str]]) -> set[str]:
    """Return modules plus all of their dependencies (transitively)."""

    seen = set(modules)
    stack = list(modules)
    while stack:
        m = stack.pop()
        for dep in module_dag.get(m, set()):
            if dep in seen:
                continue
            seen.add(dep)
            stack.append(dep)
    return seen


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


def _prepend_sys_path(dirs: Sequence[Path]) -> None:
    # Ensure discovered modules are importable.
    seen: set[str] = set(sys.path)
    for d in reversed([p.resolve() for p in dirs if p.exists()]):
        s = str(d)
        if s in seen:
            continue
        sys.path.insert(0, s)
        seen.add(s)


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


def _discover_static_targeted_test_entries(*, root: Path, cfg: JauntConfig) -> list[SpecEntry]:
    from jaunt import discovery
    from jaunt.module_contract import extract_targeted_test_entries

    test_dirs = [root / tr for tr in cfg.paths.test_roots]
    entries: list[SpecEntry] = []
    for tr, test_dir in zip(cfg.paths.test_roots, test_dirs, strict=False):
        if not test_dir.exists():
            continue
        prefix = ".".join(Path(tr).parts)
        discovered = discovery.discover_module_files(
            roots=[test_dir],
            exclude=[],
            generated_dir=cfg.paths.generated_dir,
            module_prefix=prefix or None,
        )
        for module_name, path in discovered:
            try:
                entries.extend(extract_targeted_test_entries(module_name, str(path)))
            except Exception as exc:
                raise JauntDiscoveryError(
                    f"Failed to statically inspect test module '{module_name}': "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
    return entries


def _build_backend(cfg: JauntConfig):
    if cfg.agent.engine == "aider":
        from jaunt.generate.aider_backend import AiderGeneratorBackend

        return AiderGeneratorBackend(cfg.llm, cfg.aider, cfg.prompts)

    provider = cfg.llm.provider
    if provider == "openai":
        from jaunt.generate.openai_backend import OpenAIBackend

        return OpenAIBackend(cfg.llm, cfg.prompts)
    if provider == "anthropic":
        from jaunt.generate.anthropic_backend import AnthropicBackend

        return AnthropicBackend(cfg.llm, cfg.prompts)
    if provider == "cerebras":
        from jaunt.generate.cerebras_backend import CerebrasBackend

        return CerebrasBackend(cfg.llm, cfg.prompts)
    raise JauntConfigError(
        f"Unsupported llm.provider: {provider!r}. Supported: 'openai', 'anthropic', 'cerebras'."
    )


def _is_json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json_output", False))


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _print_error(e: BaseException) -> None:
    _eprint(format_error_with_hint(e))


def _emit_json(data: dict[str, object]) -> None:
    """Write structured JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


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
# Install your chosen provider/runtime bundle:
# pip install jaunt[openai], jaunt[anthropic], or jaunt[cerebras]
provider = "openai"
model = "gpt-5.2"
api_key_env = "OPENAI_API_KEY"
# Optional: pass through provider reasoning control (OpenAI/Cerebras).
# reasoning_effort = "medium"
# Optional: Anthropic thinking budget; when set Jaunt sends
# thinking = { type = "enabled", budget_tokens = ... }.
# anthropic_thinking_budget_tokens = 1024

[agent]
engine = "aider"

[aider]
build_mode = "architect"
test_mode = "code"
skill_mode = "code"
editor_model = ""
map_tokens = 0
save_traces = false
"""


def cmd_init(args: argparse.Namespace) -> int:
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

    if json_mode:
        _emit_json({"command": "init", "ok": True, "path": str(toml_path)})

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


def cmd_status(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)

        source_dirs = [root / sr for sr in cfg.paths.source_roots]
        _prepend_sys_path([*source_dirs, root])

        from jaunt import discovery, registry
        from jaunt.deps import build_spec_graph, collapse_to_module_dag

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
        static_targeted_test_entries = _discover_static_targeted_test_entries(root=root, cfg=cfg)

        specs = dict(registry.get_magic_registry())
        if not specs:
            if json_mode:
                _emit_json(
                    {
                        "command": "status",
                        "ok": True,
                        "stale": [],
                        "fresh": [],
                    }
                )
            else:
                print("Status: 0 module(s) total")
                print("No magic specs discovered.")
            return EXIT_OK

        infer_default = bool(cfg.build.infer_deps) and (not bool(args.no_infer_deps))
        spec_graph = build_spec_graph(specs, infer_default=infer_default)
        module_dag = collapse_to_module_dag(spec_graph)
        module_specs = registry.get_specs_by_module("magic")

        package_dir = next((d for d in source_dirs if d.exists()), None)
        if package_dir is None:
            raise JauntConfigError("No existing source_roots to check.")

        from jaunt import builder
        from jaunt.generation_fingerprint import generation_fingerprint
        from jaunt.module_api import module_api_digest
        from jaunt.module_contract import group_test_entries_by_target_module

        build_generation_fingerprint = generation_fingerprint(cfg, kind="build")
        build_module_context_digests: dict[str, str] = {}
        build_module_api_digests: dict[str, str] = {}
        targeted_test_entries = group_test_entries_by_target_module(static_targeted_test_entries)
        for module_name, entries in module_specs.items():
            expected, _errs = builder._build_expected_names(entries)
            build_module_context_digests[module_name] = builder.build_module_context_artifacts(
                module_name=module_name,
                entries=entries,
                expected_names=expected,
                module_specs=module_specs,
                module_dag=module_dag,
                package_dir=package_dir,
                generated_dir=cfg.paths.generated_dir,
                targeted_test_entries=targeted_test_entries,
            ).digest
            build_module_api_digests[module_name] = module_api_digest(entries)
        stale = builder.detect_stale_modules(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            generation_fingerprint=build_generation_fingerprint,
            module_context_digests=build_module_context_digests,
            force=bool(args.force),
        )
        api_changed = builder.detect_api_changed_modules(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            module_api_digests=build_module_api_digests,
        )

        target_mods = _iter_target_modules(args.target)
        if target_mods:
            allowed = _deps_closure(target_mods, module_dag=module_dag)
            all_mods = {m for m in module_specs if m in allowed}
            api_changed = {m for m in api_changed if m in allowed}
        else:
            all_mods = set(module_specs.keys())

        stale = builder.expand_stale_modules(
            module_dag,
            stale & all_mods,
            changed_modules=api_changed,
        )
        fresh = all_mods - stale

        if json_mode:
            _emit_json(
                {
                    "command": "status",
                    "ok": True,
                    "stale": sorted(stale),
                    "fresh": sorted(fresh),
                }
            )
        else:
            stale_sorted = sorted(stale)
            fresh_sorted = sorted(fresh)
            print(f"Status: {len(all_mods)} module(s) total")
            print(f"Stale ({len(stale_sorted)}):")
            for mod in stale_sorted:
                print(f"- {mod}")
            print(f"Fresh ({len(fresh_sorted)}):")
            for mod in fresh_sorted:
                print(f"- {mod}")

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

        source_dirs = [root / sr for sr in cfg.paths.source_roots]

        skills_block = ""
        try:
            from jaunt import skills_auto

            skills_res = await skills_auto.ensure_pypi_skills_and_block(
                project_root=root,
                source_roots=[d for d in source_dirs if d.exists()],
                generated_dir=cfg.paths.generated_dir,
                llm=cfg.llm,
                agent=cfg.agent,
                aider=cfg.aider,
            )
            for w in skills_res.warnings:
                _eprint(f"warn: {w}")
            skills_block = skills_res.skills_block
        except Exception as e:  # noqa: BLE001 - best-effort; never block build
            _eprint(f"warn: failed ensuring external library skills: {type(e).__name__}: {e}")

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
        static_targeted_test_entries = _discover_static_targeted_test_entries(root=root, cfg=cfg)

        specs = dict(registry.get_magic_registry())
        if not specs:
            if json_mode:
                _emit_json(
                    {"command": "build", "ok": True, "generated": [], "skipped": [], "failed": {}}
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

        package_dir = next((d for d in source_dirs if d.exists()), None)
        if package_dir is None:
            raise JauntConfigError("No existing source_roots to build into.")

        # Lazy import so other work can land independently.
        from jaunt import builder
        from jaunt.generation_fingerprint import generation_fingerprint
        from jaunt.module_api import module_api_digest
        from jaunt.module_contract import group_test_entries_by_target_module

        build_generation_fingerprint = generation_fingerprint(cfg, kind="build")
        build_module_context_digests: dict[str, str] = {}
        build_module_api_digests: dict[str, str] = {}
        targeted_test_entries = group_test_entries_by_target_module(static_targeted_test_entries)
        for module_name, entries in module_specs.items():
            expected, _errs = builder._build_expected_names(entries)
            build_module_context_digests[module_name] = builder.build_module_context_artifacts(
                module_name=module_name,
                entries=entries,
                expected_names=expected,
                module_specs=module_specs,
                module_dag=module_dag,
                package_dir=package_dir,
                generated_dir=cfg.paths.generated_dir,
                targeted_test_entries=targeted_test_entries,
            ).digest
            build_module_api_digests[module_name] = module_api_digest(entries)
        stale = builder.detect_stale_modules(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            generation_fingerprint=build_generation_fingerprint,
            module_context_digests=build_module_context_digests,
            force=bool(args.force),
        )
        api_changed = builder.detect_api_changed_modules(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            module_api_digests=build_module_api_digests,
        )

        target_mods = _iter_target_modules(args.target)
        if target_mods:
            allowed = _deps_closure(target_mods, module_dag=module_dag)
            stale = {m for m in stale if m in allowed}
            api_changed = {m for m in api_changed if m in allowed}

        expanded_stale = builder.expand_stale_modules(
            module_dag,
            stale,
            changed_modules=api_changed,
        )

        progress = None
        if (
            expanded_stale
            and not json_mode
            and (not bool(args.no_progress))
            and sys.stderr.isatty()
        ):
            progress = ProgressBar(
                label="build",
                total=len(expanded_stale),
                enabled=True,
                stream=sys.stderr,
            )

        from jaunt.cache import ResponseCache
        from jaunt.cost import CostTracker

        cache_dir = root / ".jaunt" / "cache"
        no_cache = bool(getattr(args, "no_cache", False))
        response_cache = ResponseCache(cache_dir, enabled=not no_cache)
        cost_tracker = CostTracker(max_cost=cfg.llm.max_cost_per_build)

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
            backend=_build_backend(cfg),
            generation_fingerprint=build_generation_fingerprint,
            skills_block=skills_block,
            jobs=jobs,
            progress=progress,
            response_cache=response_cache,
            cost_tracker=cost_tracker,
            ty_retry_attempts=cfg.build.ty_retry_attempts,
            async_runner=cfg.build.async_runner,
            targeted_test_entries=targeted_test_entries,
        )

        if report.failed and not json_mode:
            _eprint(format_build_failures(report.failed))

        if not json_mode and (cost_tracker.api_calls > 0 or cost_tracker.cache_hits > 0):
            _eprint(cost_tracker.format_summary())

        if json_mode:
            _emit_json(
                {
                    "command": "build",
                    "ok": not report.failed,
                    "generated": sorted(report.generated),
                    "skipped": sorted(report.skipped),
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

        source_dirs = [root / sr for sr in cfg.paths.source_roots]
        test_dirs = [root / tr for tr in cfg.paths.test_roots]
        # Import source specs and namespace-package test modules without
        # prepending raw test roots, which can shadow stdlib/dependency imports.
        _prepend_sys_path([*source_dirs, root])

        if not bool(args.no_build):
            rc = await _cmd_build_async(args)
            if rc != EXIT_OK:
                return rc

        from jaunt import discovery, registry
        from jaunt.deps import build_spec_graph, collapse_to_module_dag
        from jaunt.module_api import build_dependency_api_block
        from jaunt.module_contract import build_module_contract, group_test_entries_by_target_module
        from jaunt.spec_ref import SpecRef

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
            magic_dependency_apis = {
                ref: build_dependency_api_block(entry) for ref, entry in build_magic_specs.items()
            }
        else:
            # cmd_build() already imported and registered magic specs.
            build_magic_specs = dict(registry.get_magic_registry())
            build_module_specs = registry.get_specs_by_module("magic")
            build_magic_spec_graph = build_spec_graph(
                build_magic_specs,
                infer_default=bool(cfg.build.infer_deps) and (not bool(args.no_infer_deps)),
            )
            build_magic_module_dag = collapse_to_module_dag(build_magic_spec_graph)
            magic_dependency_apis = {
                ref: build_dependency_api_block(entry) for ref, entry in build_magic_specs.items()
            }

        registry.clear_registries()
        modules_set: set[str] = set()
        existing_test_dirs = [d for d in test_dirs if d.exists()]
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
        if not specs:
            if json_mode:
                _emit_json({"command": "test", "ok": True, "exit_code": 0})
            return EXIT_OK
        targeted_test_entries = group_test_entries_by_target_module(list(specs.values()))

        infer_default = bool(cfg.test.infer_deps) and (not bool(args.no_infer_deps))
        spec_graph = build_spec_graph(specs, infer_default=infer_default)
        module_dag = collapse_to_module_dag(spec_graph)
        module_specs = registry.get_specs_by_module("test")

        # Lazy imports (these are layered; keep CLI import-time minimal).
        from jaunt import builder, tester
        from jaunt.generation_fingerprint import generation_fingerprint

        jobs = int(args.jobs) if args.jobs is not None else int(cfg.test.jobs)
        pytest_args = [*cfg.test.pytest_args, *list(args.pytest_args or [])]
        test_generation_fingerprint = generation_fingerprint(cfg, kind="test")
        test_module_context_digests: dict[str, str] = {}
        for module_name, entries in module_specs.items():
            expected, _errs = builder._build_expected_names(entries)
            test_module_context_digests[module_name] = build_module_contract(
                entries=entries,
                expected_names=expected,
            ).digest

        stale = tester.detect_stale_test_modules(
            project_dir=root,
            generated_dir=cfg.paths.generated_dir,
            test_roots=existing_test_dirs,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            generation_fingerprint=test_generation_fingerprint,
            module_context_digests=test_module_context_digests,
            force=bool(args.force),
        )
        stale = builder.expand_stale_modules(module_dag, stale)

        target_mods = _iter_target_modules(args.target)
        if target_mods:
            allowed = _deps_closure(target_mods, module_dag=module_dag)
            stale = {m for m in stale if m in allowed}

        progress = None
        total = len(stale & set(module_specs.keys()))
        if total and not json_mode and (not bool(args.no_progress)) and sys.stderr.isatty():
            progress = ProgressBar(label="test", total=total, enabled=True, stream=sys.stderr)

        from jaunt.cache import ResponseCache
        from jaunt.cost import CostTracker

        cache_dir = root / ".jaunt" / "cache"
        no_cache = bool(getattr(args, "no_cache", False))
        response_cache = ResponseCache(cache_dir, enabled=not no_cache)
        cost_tracker = CostTracker(max_cost=cfg.llm.max_cost_per_build)
        backend = _build_backend(cfg)
        package_dir = next((d for d in source_dirs if d.exists()), root)
        build_skills_block = ""
        try:
            from jaunt.skill_manager import build_skills_block as _build_skills_block

            build_skills_block = _build_skills_block(root)
        except Exception:
            build_skills_block = ""

        build_generation_fingerprint = generation_fingerprint(cfg, kind="build")
        repair_build_context = tester.RepairBuildContext(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=build_module_specs,
            specs=build_magic_specs,
            spec_graph=build_magic_spec_graph,
            module_dag=build_magic_module_dag,
            backend=backend,
            generation_fingerprint=build_generation_fingerprint,
            targeted_test_entries=targeted_test_entries,
            skills_block=build_skills_block,
            jobs=int(cfg.build.jobs),
            async_runner=cfg.build.async_runner,
        )

        result = tester.run_tests(
            project_dir=root,
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
        )

        if asyncio.iscoroutine(result):
            result = await result

        exit_code = int(getattr(result, "exit_code", 1))

        gen_failed = getattr(result, "generation_failed", {})
        if gen_failed and not json_mode:
            _eprint(format_test_generation_failures(gen_failed))

        if json_mode:
            _emit_json(
                {
                    "command": "test",
                    "ok": exit_code == 0 and not gen_failed,
                    "exit_code": exit_code,
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
    try:
        root, cfg = _load_config(args)
        _maybe_load_dotenv(root)

        from jaunt import eval as jaunt_eval

        compare_values = [v for group in list(args.compare or []) for v in group]
        targets = jaunt_eval.resolve_eval_targets(
            compare_values=compare_values,
            provider_override=getattr(args, "provider", None),
            model_override=getattr(args, "model", None),
            config_provider=cfg.llm.provider,
            config_model=cfg.llm.model,
        )
        suite_name = getattr(args, "suite", "codegen")
        if suite_name == "agent":
            agent_cases = jaunt_eval.load_agent_cases(list(args.case or []))
            out_root = Path(args.out).resolve() if args.out else (root / ".jaunt" / "evals")
            run_dir = jaunt_eval.make_run_dir(out_root)

            if len(targets) == 1:
                suite = jaunt_eval.run_agent_eval_suite(target=targets[0], cases=agent_cases)
                jaunt_eval.write_agent_single_target_results(suite=suite, run_dir=run_dir)

                if json_mode:
                    _emit_json(jaunt_eval.agent_suite_to_cli_json(suite=suite, run_dir=run_dir))
                else:
                    print(jaunt_eval.format_agent_suite_table(suite))
                    print(f"\nResults written to: {run_dir}")

                return EXIT_OK if suite.failed == 0 else EXIT_GENERATION_ERROR

            compare = jaunt_eval.run_agent_compare(targets=targets, cases=agent_cases)
            jaunt_eval.write_agent_compare_results(compare=compare, run_dir=run_dir)

            if json_mode:
                _emit_json(jaunt_eval.agent_compare_to_cli_json(compare=compare, run_dir=run_dir))
            else:
                print(jaunt_eval.format_agent_compare_table(compare))
                print(f"\nResults written to: {run_dir}")

            return EXIT_OK if compare.ok else EXIT_GENERATION_ERROR

        cases = jaunt_eval.load_cases(list(args.case or []))
        out_root = Path(args.out).resolve() if args.out else (root / ".jaunt" / "evals")
        run_dir = jaunt_eval.make_run_dir(out_root)

        if len(targets) == 1:
            suite = jaunt_eval.run_eval_suite(target=targets[0], cases=cases)
            jaunt_eval.write_single_target_results(suite=suite, run_dir=run_dir)

            if json_mode:
                _emit_json(jaunt_eval.suite_to_cli_json(suite=suite, run_dir=run_dir))
            else:
                print(jaunt_eval.format_suite_table(suite))
                print(f"\nResults written to: {run_dir}")

            return EXIT_OK if suite.failed == 0 else EXIT_GENERATION_ERROR

        compare = jaunt_eval.run_compare(targets=targets, cases=cases)
        jaunt_eval.write_compare_results(compare=compare, run_dir=run_dir)

        if json_mode:
            _emit_json(jaunt_eval.compare_to_cli_json(compare=compare, run_dir=run_dir))
        else:
            print(jaunt_eval.format_compare_table(compare))
            print(f"\nResults written to: {run_dir}")

        return EXIT_OK if compare.ok else EXIT_GENERATION_ERROR
    except (
        JauntConfigError,
        JauntDiscoveryError,
        JauntDependencyCycleError,
        KeyError,
        ValueError,
    ) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "eval", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY
    except (JauntGenerationError, ImportError, OSError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "eval", "ok": False, "error": str(e)})
        return EXIT_GENERATION_ERROR


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
                skills_auto.ensure_pypi_skills_and_block(
                    project_root=root,
                    source_roots=[d for d in source_dirs if d.exists()],
                    generated_dir=cfg.paths.generated_dir,
                    llm=cfg.llm,
                    agent=cfg.agent,
                    aider=cfg.aider,
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
        results = import_skills(root, from_dir=from_dir, dry_run=dry_run)
        if json_mode:
            _emit_json(
                {
                    "command": "skill import",
                    "ok": True,
                    "dry_run": dry_run,
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
        try:
            from jaunt.skill_builder import SkillBuilder

            builder = SkillBuilder(cfg.llm, cfg.agent, cfg.aider)
            updated = asyncio.run(builder.build_skill(existing, lib_contents))
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            _eprint(f"error: {msg}")
            if json_mode:
                _emit_json({"command": "skill build", "ok": False, "error": msg})
            return EXIT_GENERATION_ERROR

        # Write updated SKILL.md atomically
        from jaunt.skill_manager import skills_dir

        skill_md = skills_dir(root) / args.name / "SKILL.md"
        _atomic_write_text(skill_md, updated + "\n")

        if json_mode:
            _emit_json({"command": "skill build", "ok": True, "path": str(skill_md)})
        else:
            print(f"Updated skill: {skill_md}")
        return EXIT_OK

    return EXIT_CONFIG_OR_DISCOVERY


def cmd_mcp(args: argparse.Namespace) -> int:
    try:
        from jaunt.mcp_server import run_server

        root = getattr(args, "root", None)
        run_server(root=root)
    except ImportError:
        _eprint("error: fastmcp is not installed. Install it with: pip install jaunt[mcp]")
        return EXIT_CONFIG_OR_DISCOVERY
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
    if args.command == "eval":
        return cmd_eval(args)
    if args.command == "watch":
        return cmd_watch(args)
    if args.command == "mcp":
        return cmd_mcp(args)
    if args.command == "cache":
        return cmd_cache(args)
    if args.command in ("skill", "skills"):
        return cmd_skill(args)

    return EXIT_CONFIG_OR_DISCOVERY


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
