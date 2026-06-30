"""Render the repo map into prompt text (prompt-cache safe: no volatile fields)."""

from __future__ import annotations

from pathlib import Path

from jaunt.repo_context.tree import TreeDoc

_HEADER = "## Repository map"
_TRUNC = "  ... (repo map truncated to fit budget)"


def _lines(node: dict, prefix: str) -> list[str]:
    out: list[str] = []
    for key in sorted(node):
        if key in ("_doc", "_description", "_references", "_link"):
            continue
        val = node[key]
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(val, dict):
            doc = val.get("_doc") or val.get("_description") or ""
            out.append(f"{path}/ — {doc}" if doc else f"{path}/")
            out.extend(_lines(val, path))
        else:
            out.append(f"{path} — {val}")
    return out


def render_repo_map(treedoc: TreeDoc, *, max_chars: int = 6000) -> str:
    body_lines = _lines(treedoc.tree, "")
    if not body_lines:
        return ""
    out = _HEADER + "\n"
    kept: list[str] = []
    used = len(out)
    truncated = False
    for line in body_lines:
        if used + len(line) + 1 > max_chars:
            truncated = True
            break
        kept.append(line)
        used += len(line) + 1
    text = out + "\n".join(kept)
    if truncated:
        text += "\n" + _TRUNC
    return text


def annotate_package_tree(
    block: str, treedoc: TreeDoc, *, package_dir: Path, repo_root: Path
) -> str:
    """Append descriptions to the existing '## Package tree' lines (best-effort)."""
    if not block or "## Package tree" not in block:
        return block
    flat: dict[str, str] = {}

    def walk(node: dict, prefix: str) -> None:
        for key, val in node.items():
            if key in ("_doc", "_description", "_references", "_link"):
                continue
            path = f"{prefix}/{key}" if prefix else key
            if isinstance(val, dict):
                walk(val, path)
            else:
                flat[path] = val

    walk(treedoc.tree, "")
    try:
        pkg_rel = package_dir.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        pkg_rel = ""

    lines = block.splitlines()
    out: list[str] = []
    in_tree = False
    for line in lines:
        if line.startswith("## Package tree"):
            in_tree = True
            out.append(line)
            continue
        if in_tree and line.startswith("## "):
            in_tree = False
        if in_tree and line.strip() and "—" not in line:
            rel = f"{pkg_rel}/{line.strip()}" if pkg_rel else line.strip()
            desc = flat.get(rel)
            out.append(f"{line} — {desc}" if desc else line)
        else:
            out.append(line)
    return "\n".join(out)
