from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from jaunt import __version__
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


def _build_backend(cfg: JauntConfig):
    if cfg.llm.provider != "openai":
        raise JauntConfigError(f"Unsupported llm.provider: {cfg.llm.provider!r}")
    from jaunt.generate.openai_backend import OpenAIBackend

    return OpenAIBackend(cfg.llm, cfg.prompts)


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _print_error(e: BaseException) -> None:
    if isinstance(e, KeyError) and e.args:
        name = e.args[0]
        if isinstance(name, str) and name:
            _eprint(
                f"error: missing environment variable {name}. "
                f"Set it in the environment or add it to <project_root>/.env."
            )
            return
    msg = (str(e) or repr(e)).strip()
    _eprint(f"error: {msg}")


def _maybe_load_dotenv(root: Path) -> None:
    # Best-effort; never override existing environment variables.
    load_dotenv_into_environ(root / ".env")


def cmd_build(args: argparse.Namespace) -> int:
    try:
        root, cfg = _load_config(args)
        _maybe_load_dotenv(root)

        source_dirs = [root / sr for sr in cfg.paths.source_roots]

        skills_block = ""
        try:
            from jaunt import skills_auto

            skills_res = asyncio.run(
                skills_auto.ensure_pypi_skills_and_block(
                    project_root=root,
                    source_roots=[d for d in source_dirs if d.exists()],
                    generated_dir=cfg.paths.generated_dir,
                    llm=cfg.llm,
                )
            )
            for w in skills_res.warnings:
                _eprint(f"warn: {w}")
            skills_block = skills_res.skills_block
        except Exception as e:  # noqa: BLE001 - best-effort; never block build
            _eprint(f"warn: failed ensuring external library skills: {type(e).__name__}: {e}")

        _prepend_sys_path(source_dirs)

        from jaunt import discovery, registry
        from jaunt.deps import build_spec_graph, collapse_to_module_dag

        registry.clear_registries()
        modules = discovery.discover_modules(
            roots=[d for d in source_dirs if d.exists()],
            exclude=[],
            generated_dir=cfg.paths.generated_dir,
        )
        discovery.import_and_collect(modules, kind="magic")

        specs = dict(registry.get_magic_registry())
        if not specs:
            return EXIT_OK

        infer_default = bool(cfg.build.infer_deps) and (not bool(args.no_infer_deps))
        spec_graph = build_spec_graph(specs, infer_default=infer_default)
        module_dag = collapse_to_module_dag(spec_graph)
        module_specs = registry.get_specs_by_module("magic")

        package_dir = next((d for d in source_dirs if d.exists()), None)
        if package_dir is None:
            raise JauntConfigError("No existing source_roots to build into.")

        # Lazy import so other work can land independently.
        from jaunt import builder

        stale = builder.detect_stale_modules(
            package_dir=package_dir,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            force=bool(args.force),
        )

        target_mods = _iter_target_modules(args.target)
        if target_mods:
            allowed = _deps_closure(target_mods, module_dag=module_dag)
            stale = {m for m in stale if m in allowed}

        stale = builder.expand_stale_modules(module_dag, stale)

        progress = None
        if stale and (not bool(args.no_progress)) and sys.stderr.isatty():
            progress = ProgressBar(label="build", total=len(stale), enabled=True, stream=sys.stderr)

        jobs = int(args.jobs) if args.jobs is not None else int(cfg.build.jobs)
        report = asyncio.run(
            builder.run_build(
                package_dir=package_dir,
                generated_dir=cfg.paths.generated_dir,
                module_specs=module_specs,
                specs=specs,
                spec_graph=spec_graph,
                module_dag=module_dag,
                stale_modules=stale,
                backend=_build_backend(cfg),
                skills_block=skills_block,
                jobs=jobs,
                progress=progress,
            )
        )
        if getattr(report, "failed", None):
            return EXIT_GENERATION_ERROR
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        return EXIT_CONFIG_OR_DISCOVERY
    except (JauntGenerationError, ImportError) as e:
        _print_error(e)
        return EXIT_GENERATION_ERROR


def cmd_test(args: argparse.Namespace) -> int:
    try:
        root, cfg = _load_config(args)
        _maybe_load_dotenv(root)

        source_dirs = [root / sr for sr in cfg.paths.source_roots]
        # Test modules are expected to be importable as `tests.*` (or another
        # package under the project root). Add the project root, not the tests/
        # directory itself, so module discovery can prefix correctly.
        _prepend_sys_path([*source_dirs, root])

        if not bool(args.no_build):
            rc = cmd_build(args)
            if rc != EXIT_OK:
                return rc

        from jaunt import discovery, registry
        from jaunt.deps import build_spec_graph, collapse_to_module_dag
        from jaunt.digest import extract_source_segment
        from jaunt.spec_ref import SpecRef

        # Provide production API reference material (from @jaunt.magic) so
        # test generation can import the real APIs instead of guessing module names.
        magic_dependency_apis: dict[SpecRef, str] = {}
        if bool(args.no_build):
            registry.clear_registries()
            src_mods = discovery.discover_modules(
                roots=[d for d in source_dirs if d.exists()],
                exclude=[],
                generated_dir=cfg.paths.generated_dir,
            )
            discovery.import_and_collect(src_mods, kind="magic")
            magic_dependency_apis = {
                ref: extract_source_segment(entry)
                for ref, entry in registry.get_magic_registry().items()
            }
        else:
            # cmd_build() already imported and registered magic specs.
            magic_dependency_apis = {
                ref: extract_source_segment(entry)
                for ref, entry in registry.get_magic_registry().items()
            }

        registry.clear_registries()
        modules_set: set[str] = set()
        for tr in cfg.paths.test_roots:
            test_dir = root / tr
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
        discovery.import_and_collect(modules, kind="test")

        specs = dict(registry.get_test_registry())
        if not specs:
            return EXIT_OK

        infer_default = bool(cfg.test.infer_deps) and (not bool(args.no_infer_deps))
        spec_graph = build_spec_graph(specs, infer_default=infer_default)
        module_dag = collapse_to_module_dag(spec_graph)
        module_specs = registry.get_specs_by_module("test")

        # Lazy imports (these are layered; keep CLI import-time minimal).
        from jaunt import builder, tester

        jobs = int(args.jobs) if args.jobs is not None else int(cfg.test.jobs)
        pytest_args = [*cfg.test.pytest_args, *list(args.pytest_args or [])]

        stale = builder.detect_stale_modules(
            package_dir=root,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            force=bool(args.force),
        )
        stale = builder.expand_stale_modules(module_dag, stale)

        target_mods = _iter_target_modules(args.target)
        if target_mods:
            allowed = _deps_closure(target_mods, module_dag=module_dag)
            stale = {m for m in stale if m in allowed}

        progress = None
        total = len(stale & set(module_specs.keys()))
        if total and (not bool(args.no_progress)) and sys.stderr.isatty():
            progress = ProgressBar(label="test", total=total, enabled=True, stream=sys.stderr)

        result = tester.run_tests(
            project_dir=root,
            generated_dir=cfg.paths.generated_dir,
            dependency_apis=magic_dependency_apis,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=stale,
            backend=_build_backend(cfg),
            jobs=jobs,
            no_generate=False,
            no_run=bool(args.no_run),
            pytest_args=pytest_args,
            progress=progress,
            pythonpath=[*source_dirs, root],
            cwd=root,
        )

        if asyncio.iscoroutine(result):
            result = asyncio.run(result)

        exit_code = int(getattr(result, "exit_code", 1))
        if exit_code == 0:
            return EXIT_OK
        return EXIT_PYTEST_FAILURE if not bool(args.no_run) else EXIT_GENERATION_ERROR
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError, KeyError) as e:
        _print_error(e)
        return EXIT_CONFIG_OR_DISCOVERY
    except (JauntGenerationError, ImportError, AttributeError) as e:
        _print_error(e)
        return EXIT_GENERATION_ERROR


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

    return EXIT_CONFIG_OR_DISCOVERY


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
