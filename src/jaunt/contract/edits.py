"""Pure source transforms for adopting/ejecting a contract marker."""

from __future__ import annotations

import ast

import jaunt

jaunt.magic_module(__name__)


# Handwritten context read (but never regenerated) by the generator.
def _find_target(source: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef:
    tree = ast.parse(source)
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return node
    raise ValueError(f"top-level function or class {name!r} not found")


def _ensure_import_jaunt(lines: list[str]) -> list[str]:
    for line in lines:
        stripped = line.strip()
        if stripped == "import jaunt" or stripped.startswith("import jaunt "):
            return lines
    # Insert after a leading `from __future__` block / module docstring, else at top.
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("from __future__"):
            insert_at = i + 1
    return lines[:insert_at] + ["import jaunt"] + lines[insert_at:]


def add_contract_marker(source: str, func_name: str) -> str:
    """Return ``source`` with a ``@jaunt.contract`` decorator added to the
    top-level function or class named ``func_name``.

    This is a pure, idempotent source-to-source transform used by ``jaunt adopt``
    to mark existing committed code for contract tracking. Signature is fixed:
    ``add_contract_marker(source: str, func_name: str) -> str``.

    Procedure:

    1. Locate the target top-level ``def``/``async def``/``class`` named
       ``func_name`` using the module-level ``_find_target`` helper. If no such
       top-level definition exists, ``_find_target`` raises ``ValueError`` and
       that exception propagates out of this function unchanged.
    2. Idempotence / already-marked check. Inspect the target's existing
       decorators (``ast.unparse`` each one to its source text). If any decorator
       text is exactly ``"jaunt.contract"`` or ``"jaunt.contract()"``, OR ends
       with the suffix ``".contract"`` (e.g. an aliased ``jt.contract``), the
       target is considered already marked: return ``source`` unchanged (byte for
       byte, including its original trailing-newline state).
    3. Otherwise insert a new decorator line. The insertion anchor is the line
       number of the target's FIRST existing decorator if it has any, else the
       line number of the ``def``/``class`` statement itself. Convert this
       1-based line number to a 0-based index into ``source.splitlines()``. The
       new line reuses the anchor line's leading whitespace as ``indent`` and
       reads ``f"{indent}@jaunt.contract"``; it is inserted BEFORE the anchor line
       (so ``@jaunt.contract`` sits above any pre-existing decorators, directly
       above the first one).
    4. Ensure the module imports jaunt, via the module-level ``_ensure_import_jaunt``
       helper: if no ``import jaunt`` statement is already present, an
       ``import jaunt`` line is inserted after the last ``from __future__`` line
       (or at the very top of the file when there is none).
    5. Re-join the lines with ``"\n"``. Preserve the trailing newline: append a
       single ``"\n"`` iff the original ``source`` ended with ``"\n"``.

    Note on ``func_name``: it names a top-level function OR class; the parameter
    name is historical. ``source`` is never mutated in place.

    Examples:
    - ``add_contract_marker("async def f(x):\\n    return x\\n", "f")`` returns a
      string that contains ``"@jaunt.contract\\nasync def f(x):"`` and starts with
      ``"import jaunt"``.
    - ``add_contract_marker("class C:\\n    def m(self):\\n        return 1\\n", "C")``
      returns a string containing ``"@jaunt.contract\\nclass C:"``.
    - Given source that already decorates ``class C`` with
      ``@functools.total_ordering``, the result contains
      ``"@jaunt.contract\\n@functools.total_ordering\\nclass C:"`` (the new
      decorator is placed above the existing one).

    Raises:
        ValueError: if no top-level function or class named ``func_name`` exists.
    """
    raise NotImplementedError


def remove_contract_marker(source: str, func_name: str) -> str:
    """Return ``source`` with any ``@jaunt.contract`` decorator removed from the
    top-level function or class named ``func_name``.

    This is the inverse pure transform used by ``jaunt eject``. Signature is
    fixed: ``remove_contract_marker(source: str, func_name: str) -> str``.

    Procedure:

    1. Locate the target top-level definition named ``func_name`` via the
       module-level ``_find_target`` helper; a missing target raises ``ValueError``
       (propagated unchanged).
    2. Collect the 1-based line numbers of the target's decorators whose
       ``ast.unparse`` source text is EXACTLY ``"jaunt.contract"`` or
       ``"jaunt.contract()"``. (Unlike ``add_contract_marker``'s already-marked
       check, the loose ``".contract"`` suffix is NOT matched here — only these
       two exact spellings are removed.)
    3. If no such decorator lines were found, return ``source`` unchanged (byte
       for byte, preserving its original trailing-newline state).
    4. Otherwise drop exactly those lines from ``source.splitlines()`` (keep every
       line whose 1-based index is not in the collected set) and re-join the
       remaining lines with ``"\n"``. Preserve the trailing newline: append a
       single ``"\n"`` iff the original ``source`` ended with ``"\n"``.

    This function removes ONLY the ``@jaunt.contract`` decorator line(s); it does
    NOT remove any ``import jaunt`` line that ``add_contract_marker`` may have
    added (so a strict add→remove round trip differs from the original by that
    lone ``import jaunt`` line).

    Examples:
    - Round trip: for ``src = "class C:\\n    def m(self):\\n        return 1\\n"``,
      ``remove_contract_marker(add_contract_marker(src, "C"), "C")`` equals ``src``
      except for a leading ``"import jaunt\\n"`` line (i.e. removing that line from
      the result reproduces ``src`` exactly).
    - ``remove_contract_marker("def f():\\n    pass\\n", "f")`` returns the input
      unchanged (no contract decorator present).

    Raises:
        ValueError: if no top-level function or class named ``func_name`` exists.
    """
    raise NotImplementedError
