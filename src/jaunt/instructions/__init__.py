"""The `jaunt instructions` agent primer.

A harness-agnostic, project-aware briefing a coding agent loads before operating
Jaunt. The static text lives in ``primer.md`` (the single canonical source the
``SKILL.md`` stubs point to); the command surface is rendered from the
``COMMANDS`` / ``EXIT_CODES`` data below so a drift-guard test can verify it
against the real CLI; and ``project_section`` adds a live snapshot of the current
project's config and build freshness.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from jaunt.generate.shared import render_template
from jaunt.init_template import FULL_SCHEMA_TEMPLATE

if TYPE_CHECKING:
    from jaunt.config import JauntConfig

# Curated, ranked command surface shown in the primer. Entries map to real CLI
# commands; `test_instructions` asserts top-level coverage stays in sync with the parser.
COMMANDS: list[tuple[str, str]] = [
    ("build", "Generate implementations for `@jaunt.magic` specs."),
    ("test", "Generate tests for `@jaunt.test` specs and run pytest."),
    ("status", "Show which modules are stale vs fresh (and why)."),
    ("doctor", "Run read-only environment and workspace health checks."),
    ("sync", "Render TypeScript API mirrors and unbuilt placeholders without a model call."),
    ("design", "Propose or apply a reviewed TypeScript public declaration."),
    ("specs", "List `@jaunt.magic` specs and their dependency graph."),
    ("log", "Show the `JAUNT_LOG` change journal (recent builds/adopts)."),
    ("daemon", "Run, stop, or inspect the background codegen daemon."),
    ("jobs", "List daemon jobs, inspect parked failures, or retry landing."),
    (
        "jobs wait",
        "Block for daemon completion: 0 green, 4 failed/parked, 5 timeout, 2 daemon-not-running.",
    ),
    ("watch", "Rebuild (and optionally test) on file changes."),
    ("init", "Scaffold `jaunt.toml` + source/test directories."),
    ("clean", "Remove `__generated__/` directories."),
    ("migrate", "Plan/apply mechanical source migrations without model calls."),
    ("instructions", "Print this agent primer (project-aware)."),
    ("adopt", "Track existing code with `@jaunt.contract` and derive its battery."),
    ("reconcile", "Derive/refresh committed contract batteries (calls the model)."),
    ("check", "Verify committed contract batteries deterministically (CI gate)."),
    ("eject", "Remove contract tracking; leave plain Python + pytest."),
    ("guard", "Warn-on-access PreToolUse hook for `__generated__/` (see docs/hooks.md)."),
]

# Real subcommands intentionally NOT surfaced in the primer (advanced / rare).
# The drift-guard test requires every real subcommand to be in COMMANDS or here,
# so adding a new command forces an explicit decision.
OMITTED_COMMANDS: frozenset[str] = frozenset(
    {
        "tree",
        "eval",
        "cache",
        "skill",
        "skills",
        "install-claude-plugin",
        "install-codex-plugin",
    }
)

EXIT_CODES: list[tuple[int, str]] = [
    (0, "Success."),
    (2, "Config, discovery, or dependency-cycle error."),
    (3, "Code generation error."),
    (4, "Pytest failure, contract `check`/`reconcile` block, or daemon job failed/parked."),
    (5, "Timeout while waiting for daemon jobs."),
]


def load_primer() -> str:
    """Return the raw static primer text (with `{{...}}` placeholders intact)."""
    return (resources.files("jaunt") / "instructions" / "primer.md").read_text(encoding="utf-8")


def _command_table() -> str:
    rows = ["| Command | What it does |", "|---------|--------------|"]
    rows += [f"| `jaunt {name}` | {blurb} |" for name, blurb in COMMANDS]
    return "\n".join(rows)


def _exit_code_table() -> str:
    rows = ["| Code | Meaning |", "|------|---------|"]
    rows += [f"| {code} | {meaning} |" for code, meaning in EXIT_CODES]
    return "\n".join(rows)


def _typescript_tool_metadata(root: Path, tool_owner: str) -> dict[str, str]:
    owner = (root / tool_owner).resolve()
    package_manager = "unknown"
    try:
        package = json.loads((owner / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        package = {}
    if isinstance(package, dict) and isinstance(package.get("packageManager"), str):
        package_manager = str(package["packageManager"])
    else:
        for lockfile, name in (
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("bun.lock", "bun"),
            ("bun.lockb", "bun"),
            ("package-lock.json", "npm"),
            ("npm-shrinkwrap.json", "npm"),
        ):
            if (owner / lockfile).is_file() or (root / lockfile).is_file():
                package_manager = name
                break

    def version(package_path: Path) -> str:
        current = owner
        while current == root or current.is_relative_to(root):
            path = current / "node_modules" / package_path / "package.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict) and isinstance(value.get("version"), str):
                return str(value["version"])
            if current == root:
                break
            current = current.parent
        return "unresolved"

    return {
        "package_manager": package_manager,
        "worker_version": version(Path("@usejaunt") / "ts"),
        "typescript_version": version(Path("typescript")),
    }


def render(*, project: dict | None, note: str | None = None) -> str:
    """Render the full primer markdown: static text + live project section."""
    text = render_template(
        load_primer(),
        {"COMMAND_TABLE": _command_table(), "EXIT_CODES": _exit_code_table()},
    )
    return text.rstrip() + "\n\n" + _project_block(project, note) + "\n"


def _project_block(project: dict | None, note: str | None) -> str:
    lines = ["## Your project right now", ""]
    if project is None:
        lines.append(f"> {note or 'No jaunt.toml found — run `jaunt init` to start.'}")
        lines += [
            "",
            "## jaunt.toml schema",
            "",
            "```toml",
            FULL_SCHEMA_TEMPLATE.rstrip("\n"),
            "```",
        ]
        return "\n".join(lines)

    paths = project["paths"]
    src = " · ".join(f"`{p}`" for p in paths["source_roots"]) or "(none)"
    tst = " · ".join(f"`{p}`" for p in paths["test_roots"]) or "(none)"
    gate = project["semantic_gate"]
    gate_txt = f"enabled (`{gate['model']}`)" if gate.get("enabled") else "disabled"
    lines += [
        f"- **Root:** `{project['root']}`",
        f"- **Source roots:** {src}",
        f"- **Test roots:** {tst}",
        f"- **Generated dir:** `{paths['generated_dir']}`",
        f"- **Engine:** `{project['engine']}` · "
        f"**Model:** `{project['model']}` (effort: {project['reasoning_effort']})",
        f"- **Semantic gate:** {gate_txt}",
        f"- **Repo map:** {'on' if project['repo_map'] else 'off'}",
    ]
    targets = project.get("targets")
    if isinstance(targets, dict):
        lines.insert(1, f"- **Target languages:** {', '.join(f'`{item}`' for item in targets)}")
        typescript = targets.get("ts")
        if isinstance(typescript, dict):
            projects = " · ".join(f"`{item}`" for item in typescript.get("projects", []))
            lines.append(f"- **TypeScript projects:** {projects or '(none)'}")
            lines.append(f"- **TypeScript tool owner:** `{typescript.get('tool_owner', '.')}`")
            lines.append(
                "- **TypeScript tooling:** "
                f"`@usejaunt/ts {typescript.get('worker_version', 'unresolved')}` · "
                f"`TypeScript {typescript.get('typescript_version', 'unresolved')}` · "
                f"package manager `{typescript.get('package_manager', 'unknown')}`"
            )

    fresh = project["freshness"]
    typescript_target = targets.get("ts") if isinstance(targets, dict) else None
    freshness_label = (
        "Python build freshness"
        if isinstance(fresh, dict) and fresh.get("scope") == "py"
        else "Build freshness"
    )
    if fresh is None:
        if not isinstance(typescript_target, dict):
            lines.append("- **Build freshness:** unavailable here — run `jaunt status`.")
    elif fresh["total"] == 0:
        lines.append(f"- **{freshness_label}:** no `@jaunt.magic` specs discovered yet.")
    else:
        suffix = " — run `jaunt build`" if fresh["stale"] else ""
        lines.append(
            f"- **{freshness_label}:** {fresh['fresh']} fresh, {fresh['stale']} stale{suffix}."
        )
        if fresh["stale_modules"]:
            shown = ", ".join(f"`{m}`" for m in fresh["stale_modules"])
            more = "" if fresh["stale"] <= len(fresh["stale_modules"]) else ", …"
            lines.append(f"  - stale: {shown}{more}")
    if isinstance(typescript_target, dict):
        lines.append(
            "- **TypeScript build freshness:** not probed by `jaunt instructions` — "
            "run `jaunt status --language ts` for the authoritative result."
        )
    return "\n".join(lines)


def project_section(root: Path, cfg: JauntConfig) -> dict:
    """Build the structured live snapshot of the current project.

    This is also the `project` payload emitted under `--json`. The freshness
    probe is best-effort: any failure yields ``freshness == None`` rather than
    raising, so the primer always renders.
    """
    project: dict[str, object] = {
        "root": str(root),
        "paths": {
            "source_roots": list(cfg.paths.source_roots),
            "test_roots": list(cfg.paths.test_roots),
            "generated_dir": cfg.paths.generated_dir,
        },
        "engine": cfg.agent.engine,
        "model": cfg.codex.model,
        "reasoning_effort": cfg.codex.reasoning_effort,
        "semantic_gate": {
            "enabled": bool(cfg.semantic_gate.enabled),
            "model": cfg.semantic_gate.model,
        },
        "repo_map": bool(cfg.context.repo_map),
        "freshness": _freshness(root, cfg),
    }
    if cfg.version == 2:
        targets: dict[str, object] = {}
        if cfg.python_target is not None:
            targets["py"] = {
                "source_roots": list(cfg.python_target.source_roots),
                "test_roots": list(cfg.python_target.test_roots),
                "generated_dir": cfg.python_target.generated_dir,
            }
        if cfg.typescript_target is not None:
            tool_metadata = _typescript_tool_metadata(
                root.resolve(), cfg.typescript_target.tool_owner
            )
            targets["ts"] = {
                "source_roots": list(cfg.typescript_target.source_roots),
                "test_roots": list(cfg.typescript_target.test_roots),
                "projects": list(cfg.typescript_target.projects),
                "test_projects": list(cfg.typescript_target.test_projects),
                "tool_owner": cfg.typescript_target.tool_owner,
                "generated_dir": cfg.typescript_target.generated_dir,
                **tool_metadata,
            }
        project["targets"] = targets
    return project


def _freshness(root: Path, cfg: JauntConfig) -> dict | None:
    """Best-effort stale/fresh summary; None if it cannot be computed cleanly."""
    if cfg.version == 2 and cfg.python_target is None:
        return None
    try:
        from jaunt.status_core import compute_magic_status

        status = compute_magic_status(
            root=root,
            cfg=cfg,
            source_dirs=[root / sr for sr in cfg.paths.source_roots],
            build_instructions=list(cfg.build.instructions),
            include_target_tests=bool(cfg.build.include_target_tests),
            infer_deps=bool(cfg.build.infer_deps),
            force=False,
            target=(),
        )
    except Exception:  # noqa: BLE001 - never let the probe break the primer
        return None
    return {
        **({"scope": "py"} if cfg.version == 2 else {}),
        "total": status.total,
        "fresh": len(status.fresh),
        "stale": len(status.stale),
        "stale_modules": sorted(status.stale)[:5],
    }


def no_project_note(error_message: str) -> str:
    """Friendly note for the no-/unloadable-config case."""
    msg = error_message.lower()
    missing = "jaunt.toml" in msg and any(
        marker in msg for marker in ("missing", "could not find", "not found", "no such")
    )
    if missing:
        return "No jaunt.toml found — run `jaunt init` to start. (Showing framework rules only.)"
    return f"jaunt.toml could not be loaded ({error_message}). Showing framework rules only."
