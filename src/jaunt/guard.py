"""PreToolUse guard: warn when an agent reads/edits machine-owned generated code."""

from __future__ import annotations

_FILE_KEYS = ("file_path", "path", "notebook_path")
_FILE_TOOLS = frozenset({"Edit", "Write", "Read", "NotebookEdit", "MultiEdit"})


def _owning_spec_hint(path: str, generated_dir: str) -> str:
    parts = path.split("/")
    if generated_dir in parts:
        idx = parts.index(generated_dir)
        return "/".join(parts[:idx] + parts[idx + 1 :])
    return path


def evaluate(payload: dict, *, generated_dir: str) -> dict | None:
    try:
        if payload.get("tool_name") not in _FILE_TOOLS:
            return None
        tool_input = payload.get("tool_input") or {}
        path = next((str(tool_input[k]) for k in _FILE_KEYS if tool_input.get(k)), None)
    except (AttributeError, TypeError):
        return None
    if not path or not generated_dir or f"/{generated_dir}/" not in f"/{path}":
        return None
    spec_hint = _owning_spec_hint(path, generated_dir)
    reason = (
        f"{path} is machine-owned generated code (jaunt). Edit the spec instead: "
        f"{spec_hint}. Changes here are overwritten on the next build."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }
