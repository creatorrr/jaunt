"""Pure logic for ``jaunt install-claude-plugin``.

Builds the ``claude plugin ...`` argument lists and classifies completed
subprocess results without touching the filesystem or spawning processes, so
the orchestration in ``cli.py`` stays thin and this stays unit-testable.
"""

from __future__ import annotations

MARKETPLACE_REF = "creatorrr/jaunt"
MARKETPLACE_NAME = "jaunt-plugins"
PLUGIN_REF = "jaunt@jaunt-plugins"
DOCS_URL = "https://jaunt.ing/docs/guides/claude-code-plugin"


def marketplace_add_command(*, local_path: str | None) -> list[str]:
    """argv for ``claude plugin marketplace add``.

    ``local_path`` points at a local clone's root; ``None`` adds the GitHub
    marketplace ``creatorrr/jaunt``.
    """
    target = local_path if local_path is not None else MARKETPLACE_REF
    return ["claude", "plugin", "marketplace", "add", target]


def plugin_install_command() -> list[str]:
    """argv for ``claude plugin install jaunt@jaunt-plugins``."""
    return ["claude", "plugin", "install", PLUGIN_REF]


def marketplace_update_command() -> list[str]:
    """argv for refreshing the configured marketplace before a plugin update."""
    return ["claude", "plugin", "marketplace", "update", MARKETPLACE_NAME]


def plugin_update_command() -> list[str]:
    """argv for updating an existing Jaunt plugin installation."""
    return ["claude", "plugin", "update", PLUGIN_REF]


def classify_result(returncode: int, stdout: str, stderr: str) -> str:
    """Classify a completed subprocess result.

    Returns ``"ok"`` on a clean exit, ``"already"`` when the command failed only
    because the marketplace/plugin was already present (idempotent re-run), and
    ``"error"`` for any other failure.
    """
    combined = f"{stdout}\n{stderr}".lower()
    if "different source" in combined or "remove it before" in combined:
        return "error"
    if "already" in combined:
        return "already"
    if returncode == 0:
        return "ok"
    return "error"


def missing_cli_message() -> str:
    """Actionable error shown when the ``claude`` CLI is not on PATH."""
    return (
        "Claude Code CLI not found on PATH. Install Claude Code, then run these "
        "two commands manually:\n"
        f"  claude plugin marketplace add {MARKETPLACE_REF}\n"
        f"  claude plugin install {PLUGIN_REF}\n"
        f"See {DOCS_URL}"
    )
