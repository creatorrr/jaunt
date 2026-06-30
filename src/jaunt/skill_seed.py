"""Seed Agent-Skills into a Codex workspace so `codex exec` discovers them."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Sequence
from pathlib import Path

from jaunt.skills_builtin import iter_enabled_builtin_skill_dirs


def _project_skill_dirs(project_root: Path | None) -> list[tuple[str, Path]]:
    if project_root is None:
        return []

    from jaunt.skill_manager import skills_dir

    sd = skills_dir(project_root)
    if not sd.is_dir():
        return []

    pairs: list[tuple[str, Path]] = []
    for skill_md in sorted(sd.glob("*/SKILL.md")):
        pairs.append((skill_md.parent.name, skill_md.parent))
    return pairs


def seed_skills_into_workspace(
    workspace_root: Path,
    *,
    project_root: Path | None,
    builtin_names: Sequence[str],
) -> list[str]:
    """Copy builtin + project skill dirs into <workspace_root>/.agents/skills/.

    Project skills override builtins of the same name. Best-effort: a failure to
    copy one dir is recorded as a warning and does not abort the rest.
    """
    warnings: list[str] = []
    dest_root = workspace_root / ".agents" / "skills"

    ordered: dict[str, Path] = {}
    for name, src in iter_enabled_builtin_skill_dirs(builtin_names):
        ordered[name] = src
    for name, src in _project_skill_dirs(project_root):
        ordered[name] = src

    for name, src in ordered.items():
        dest = dest_root / name
        try:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        except Exception as e:  # noqa: BLE001 - best-effort seeding
            warnings.append(f"failed seeding skill {name!r}: {type(e).__name__}: {e}")
    return warnings


def skills_fingerprint(
    *,
    project_root: Path | None,
    builtin_names: Sequence[str],
) -> str:
    """Stable digest over the seeded skill set (names + file contents)."""
    h = hashlib.sha256()
    ordered: dict[str, Path] = {}
    for name, src in iter_enabled_builtin_skill_dirs(builtin_names):
        ordered[name] = src
    for name, src in _project_skill_dirs(project_root):
        ordered[name] = src

    for name in sorted(ordered):
        skill_dir = ordered[name]
        h.update(name.encode())
        h.update(b"\0")
        for f in sorted(skill_dir.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(skill_dir)).encode())
                h.update(b"\0")
                try:
                    h.update(f.read_bytes())
                except Exception:  # noqa: BLE001
                    pass
                h.update(b"\0")
        h.update(b"\1")
    return h.hexdigest()
