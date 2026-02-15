"""MCP server for Jaunt — exposes build/test/status/clean/spec_info as MCP tools.

The server uses FastMCP (optional dependency) for the transport layer.
Core tool functions are plain Python and can be tested without FastMCP installed.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import jaunt.cli

# ---------------------------------------------------------------------------
# Core tool functions (no FastMCP dependency)
# ---------------------------------------------------------------------------


def _run_cli_json(argv: list[str]) -> str:
    """Run a CLI command with --json and capture its stdout JSON output.

    Some commands (e.g. ``test``) internally call other commands (``build``)
    that also emit JSON.  We extract only the *last* JSON object from the
    captured output so callers always get a single, valid JSON document.

    Returns the JSON string emitted by the command.  If the command produces no
    stdout (e.g. an error path that only prints to stderr), we synthesise an
    error envelope so callers always get valid JSON.
    """
    cmd_name = argv[0] if argv else "unknown"
    buf = io.StringIO()
    with redirect_stdout(buf):
        jaunt.cli.main(argv)
    output = buf.getvalue().strip()
    if not output:
        return json.dumps({"command": cmd_name, "ok": False, "error": "command produced no output"})

    # When multiple JSON documents are emitted (e.g. build + test), keep the
    # last one — it corresponds to the top-level command the caller invoked.
    # Each JSON doc starts with '{' at column 0 after a newline boundary.
    last_start = output.rfind("\n{")
    candidate = output[last_start + 1 :] if last_start != -1 else output

    # Validate that the output is actually JSON.  If the CLI produced non-JSON
    # text (e.g. argparse help), wrap it in an error envelope.
    try:
        json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return json.dumps({"command": cmd_name, "ok": False, "error": candidate[:500]})
    return candidate


def tool_build(
    *,
    root: str | None = None,
    force: bool = False,
    target: str | None = None,
    jobs: int | None = None,
) -> str:
    """Build specs and return structured results."""
    argv = ["build", "--json"]
    if root:
        argv += ["--root", root]
    if force:
        argv.append("--force")
    if target:
        argv += ["--target", target]
    if jobs is not None:
        argv += ["--jobs", str(jobs)]
    return _run_cli_json(argv)


def tool_test(
    *,
    root: str | None = None,
    force: bool = False,
    no_build: bool = False,
    no_run: bool = False,
    target: str | None = None,
    jobs: int | None = None,
) -> str:
    """Generate and run tests."""
    argv = ["test", "--json"]
    if root:
        argv += ["--root", root]
    if force:
        argv.append("--force")
    if no_build:
        argv.append("--no-build")
    if no_run:
        argv.append("--no-run")
    if target:
        argv += ["--target", target]
    if jobs is not None:
        argv += ["--jobs", str(jobs)]
    return _run_cli_json(argv)


def tool_status(
    *,
    root: str | None = None,
    target: str | None = None,
) -> str:
    """Check which modules are stale."""
    argv = ["status", "--json"]
    if root:
        argv += ["--root", root]
    if target:
        argv += ["--target", target]
    return _run_cli_json(argv)


def tool_clean(
    *,
    root: str | None = None,
    dry_run: bool = False,
) -> str:
    """Clean generated files."""
    argv = ["clean", "--json"]
    if root:
        argv += ["--root", root]
    if dry_run:
        argv.append("--dry-run")
    return _run_cli_json(argv)


def tool_spec_info(
    *,
    root: str | None = None,
    module: str | None = None,
) -> str:
    """Return specs and dependency graph for the project (or a specific module)."""
    saved_path = sys.path[:]
    try:
        from jaunt.config import find_project_root, load_config

        root_path = Path(root).resolve() if root else find_project_root(Path.cwd())
        cfg = load_config(root=root_path)

        source_dirs = [root_path / sr for sr in cfg.paths.source_roots]

        # Temporarily add source dirs so modules are importable.
        seen: set[str] = set(sys.path)
        for d in reversed([p.resolve() for p in source_dirs if p.exists()]):
            s = str(d)
            if s not in seen:
                sys.path.insert(0, s)
                seen.add(s)

        from jaunt import discovery, registry
        from jaunt.deps import build_spec_graph

        registry.clear_registries()
        modules = discovery.discover_modules(
            roots=[d for d in source_dirs if d.exists()],
            exclude=[],
            generated_dir=cfg.paths.generated_dir,
        )
        discovery.import_and_collect(modules, kind="magic")

        specs = dict(registry.get_magic_registry())
        spec_graph = build_spec_graph(specs, infer_default=cfg.build.infer_deps)

        # Build spec list, optionally filtered by module.
        spec_list = []
        for ref, entry in sorted(specs.items()):
            if module and entry.module != module:
                continue
            spec_list.append(
                {
                    "ref": str(ref),
                    "module": entry.module,
                    "qualname": entry.qualname,
                    "source_file": entry.source_file,
                }
            )

        # Build dependency graph as JSON-serializable dict.
        dep_graph: dict[str, list[str]] = {}
        for ref, deps in sorted(spec_graph.items()):
            if module and not str(ref).startswith(module + ":"):
                continue
            dep_graph[str(ref)] = sorted(str(d) for d in deps)

        return json.dumps(
            {
                "command": "spec_info",
                "ok": True,
                "specs": spec_list,
                "dependency_graph": dep_graph,
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {
                "command": "spec_info",
                "ok": False,
                "error": str(e),
            }
        )
    finally:
        sys.path[:] = saved_path


# ---------------------------------------------------------------------------
# FastMCP server factory
# ---------------------------------------------------------------------------


def create_mcp_server():
    """Create and return a FastMCP server with jaunt tools registered.

    Raises ImportError if fastmcp is not installed.
    """
    from fastmcp import FastMCP

    mcp = FastMCP("jaunt", instructions="Jaunt spec-driven code generation framework")

    @mcp.tool()
    def jaunt_build(
        root: str | None = None,
        force: bool = False,
        target: str | None = None,
        jobs: int | None = None,
    ) -> str:
        """Build specs and return structured results.

        Generates implementations for @jaunt.magic decorated spec stubs.
        Returns JSON with generated/skipped/failed module lists.
        """
        return tool_build(root=root, force=force, target=target, jobs=jobs)

    @mcp.tool()
    def jaunt_test(
        root: str | None = None,
        force: bool = False,
        no_build: bool = False,
        no_run: bool = False,
        target: str | None = None,
        jobs: int | None = None,
    ) -> str:
        """Generate and run tests for @jaunt.test spec stubs.

        Returns JSON with test exit code and pass/fail status.
        """
        return tool_test(
            root=root, force=force, no_build=no_build, no_run=no_run, target=target, jobs=jobs
        )

    @mcp.tool()
    def jaunt_status(
        root: str | None = None,
        target: str | None = None,
    ) -> str:
        """Check which modules are stale vs fresh.

        Returns JSON with lists of stale and fresh module names.
        """
        return tool_status(root=root, target=target)

    @mcp.tool()
    def jaunt_spec_info(
        root: str | None = None,
        module: str | None = None,
    ) -> str:
        """Return specs and dependency graph for the project.

        Optionally filter by module name. Returns JSON with spec list
        and dependency graph.
        """
        return tool_spec_info(root=root, module=module)

    @mcp.tool()
    def jaunt_clean(
        root: str | None = None,
        dry_run: bool = False,
    ) -> str:
        """Clean generated files from __generated__ directories.

        Use dry_run=True to preview what would be removed without deleting.
        Returns JSON with list of removed (or would-be-removed) paths.
        """
        return tool_clean(root=root, dry_run=dry_run)

    return mcp


def run_server(*, root: str | None = None) -> None:
    """Entry point: create and run the MCP server (stdio transport).

    If *root* is provided, changes the working directory to that path so
    that all tools resolve relative to the given project root.
    """
    import os

    if root:
        os.chdir(Path(root).resolve())
    mcp = create_mcp_server()
    mcp.run()
