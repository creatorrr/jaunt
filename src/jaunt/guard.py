"""PreToolUse guard: warn when an agent reads/edits machine-owned generated code."""

from __future__ import annotations

import jaunt

jaunt.magic_module(__name__)

# Handwritten context read (but never regenerated) by the generator.
_FILE_KEYS = ("file_path", "path", "notebook_path")
_FILE_TOOLS = frozenset({"Edit", "Write", "Read", "NotebookEdit", "MultiEdit"})


def _owning_spec_hint(path: str, generated_dir: str) -> str:
    parts = path.split("/")
    if generated_dir in parts:
        idx = parts.index(generated_dir)
        return "/".join(parts[:idx] + parts[idx + 1 :])
    return path


def evaluate(payload: dict, *, generated_dir: str) -> dict | None:
    """Decide whether a PreToolUse tool call touches jaunt-generated code.

    This is the implementation of the ``jaunt guard`` PreToolUse hook. Given the
    raw hook ``payload`` and the project's configured ``generated_dir`` name
    (e.g. ``"__generated__"``), it returns either ``None`` (no objection — let
    the tool run) or a hook-output dict that asks the user to confirm before
    editing machine-owned generated code.

    Signature is fixed: ``evaluate(payload: dict, *, generated_dir: str) -> dict | None``.

    Decision procedure (return ``None`` at the first step that does not match):

    1. Only file-touching tools are considered. Read ``payload["tool_name"]``;
       if it is not one of the file-tool names — the module-level ``_FILE_TOOLS``
       set: ``"Edit"``, ``"Write"``, ``"Read"``, ``"NotebookEdit"``,
       ``"MultiEdit"`` — return ``None``. (A ``"Bash"`` call, for example, is
       never flagged even if its input mentions a generated path.)
    2. Extract the target path from ``payload["tool_input"]`` (treat a missing or
       falsy ``tool_input`` as an empty mapping). Check the candidate keys in the
       module-level ``_FILE_KEYS`` order — ``"file_path"``, then ``"path"``, then
       ``"notebook_path"`` — and take the first key whose value is truthy,
       coercing that value to ``str``. If none is present, the path is absent.
    3. Malformed payloads must never raise. If accessing ``tool_name`` /
       ``tool_input`` / a candidate key raises ``AttributeError`` or ``TypeError``
       (e.g. ``payload`` or ``tool_input`` is not a mapping), return ``None``.
    4. Return ``None`` if no path was found, if ``generated_dir`` is empty/falsy,
       or if the path does not contain the generated directory as a path
       segment. The segment test is exactly: ``f"/{generated_dir}/"`` is a
       substring of ``f"/{path}"`` (so ``generated_dir`` must appear delimited by
       ``/`` on both sides; a bare prefix or suffix match does not count).

    When all checks pass, the path is machine-owned generated code. Compute the
    owning spec path with the module-level helper
    ``_owning_spec_hint(path, generated_dir)`` (which drops the ``generated_dir``
    segment from the path), then build the warning message exactly as::

        f"{path} is machine-owned generated code (jaunt). Edit the spec instead: "
        f"{spec_hint}. Changes here are overwritten on the next build."

    and return::

        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": <the message above>,
            }
        }

    ``payload`` and ``generated_dir`` are read at call time; nothing here depends
    on module-level mutable state beyond the two frozen constants above.

    Examples:
    - ``evaluate({"tool_name": "Edit", "tool_input": {"file_path":
      "src/pkg/__generated__/mod.py"}}, generated_dir="__generated__")`` returns a
      dict whose ``hookSpecificOutput.permissionDecision`` is ``"ask"`` and whose
      ``permissionDecisionReason`` contains the spec hint ``"src/pkg/mod.py"``.
    - ``evaluate({"tool_name": "Edit", "tool_input": {"file_path":
      "src/pkg/mod.py"}}, generated_dir="__generated__")`` returns ``None`` (path
      is not under the generated dir).
    - ``evaluate({"tool_name": "Bash", "tool_input": {"file_path":
      "src/__generated__/mod.py"}}, generated_dir="__generated__")`` returns
      ``None`` (not a file tool).
    - ``evaluate({}, generated_dir="__generated__")`` and
      ``evaluate({"tool_input": None}, generated_dir="__generated__")`` both
      return ``None`` (malformed / empty payloads never raise).
    """
    raise NotImplementedError
