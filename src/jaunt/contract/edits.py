"""Pure source transforms for adopting/ejecting a contract marker."""

from __future__ import annotations

import ast


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
    target = _find_target(source, func_name)

    # Already marked?
    for dec in target.decorator_list:
        text = ast.unparse(dec)
        if text in ("jaunt.contract", "jaunt.contract()") or text.endswith(".contract"):
            return source

    lines = source.splitlines()
    # Insert above the first decorator if present, else above `def`.
    anchor = target.decorator_list[0].lineno if target.decorator_list else target.lineno
    insert_idx = anchor - 1  # 1-based lineno -> 0-based index
    indent = lines[insert_idx][: len(lines[insert_idx]) - len(lines[insert_idx].lstrip())]
    lines.insert(insert_idx, f"{indent}@jaunt.contract")

    lines = _ensure_import_jaunt(lines)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def remove_contract_marker(source: str, func_name: str) -> str:
    target = _find_target(source, func_name)
    targets = set()
    for dec in target.decorator_list:
        text = ast.unparse(dec)
        if text in ("jaunt.contract", "jaunt.contract()"):
            targets.add(dec.lineno)
    if not targets:
        return source
    lines = source.splitlines()
    kept = [line for i, line in enumerate(lines, start=1) if i not in targets]
    return "\n".join(kept) + ("\n" if source.endswith("\n") else "")
