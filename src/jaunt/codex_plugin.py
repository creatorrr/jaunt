"""Pure logic for ``jaunt install-codex-plugin``."""

from __future__ import annotations

MARKETPLACE_REF = "creatorrr/jaunt"
MARKETPLACE_NAME = "jaunt-codex-plugins"
PLUGIN_REF = f"jaunt@{MARKETPLACE_NAME}"
DOCS_URL = "https://jaunt.ing/docs/guides/codex-plugin"


def marketplace_add_command(*, local_path: str | None) -> list[str]:
    """Return argv for adding the GitHub or local Codex marketplace."""
    target = local_path if local_path is not None else MARKETPLACE_REF
    return ["codex", "plugin", "marketplace", "add", target]


def plugin_install_command() -> list[str]:
    """Return argv for installing the Jaunt Codex plugin."""
    return ["codex", "plugin", "add", PLUGIN_REF]


def classify_result(returncode: int, stdout: str, stderr: str) -> str:
    """Classify success, an idempotent already-present result, or failure."""
    if returncode == 0:
        return "ok"
    if "already" in f"{stdout}\n{stderr}".lower():
        return "already"
    return "error"


def missing_cli_message() -> str:
    """Return an actionable error when the Codex CLI is unavailable."""
    return (
        "Codex CLI not found on PATH. Install Codex, then run these two commands "
        "manually:\n"
        f"  codex plugin marketplace add {MARKETPLACE_REF}\n"
        f"  codex plugin add {PLUGIN_REF}\n"
        f"See {DOCS_URL}"
    )
