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

    from jaunt.skill_manager import managed_skills_dir, parse_managed_skill_meta, skills_dir

    ordered: dict[str, Path] = {}
    # Classic managed skills are a compatibility fallback until explicitly migrated.
    user_root = skills_dir(project_root)
    if user_root.is_dir():
        for skill_md in sorted(user_root.glob("*/SKILL.md")):
            try:
                managed = parse_managed_skill_meta(skill_md.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if managed is not None:
                ordered[skill_md.parent.name] = skill_md.parent
    managed_root = managed_skills_dir(project_root)
    if managed_root.is_dir():
        for skill_md in sorted(managed_root.glob("*/SKILL.md")):
            ordered[skill_md.parent.name] = skill_md.parent
    # User skills intentionally override managed skills of the same name.
    if user_root.is_dir():
        for skill_md in sorted(user_root.glob("*/SKILL.md")):
            try:
                managed = parse_managed_skill_meta(skill_md.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if managed is None:
                ordered[skill_md.parent.name] = skill_md.parent
    return list(ordered.items())


def _resolved_skill_dirs(
    *,
    project_root: Path | None,
    builtin_names: Sequence[str],
    selected_names: Sequence[str] | None = None,
) -> dict[str, Path]:
    ordered: dict[str, Path] = {}
    for name, src in iter_enabled_builtin_skill_dirs(builtin_names):
        ordered[name] = src
    for name, src in _project_skill_dirs(project_root):
        ordered[name] = src
    if selected_names is not None:
        selected = set(selected_names)
        ordered = {name: path for name, path in ordered.items() if name in selected}
    return ordered


def skills_workspace_stats(
    *,
    project_root: Path | None,
    builtin_names: Sequence[str],
    selected_names: Sequence[str] | None = None,
) -> tuple[int, int]:
    """Return ``(skill_count, SKILL.md chars)`` for the workspace Jaunt will seed."""
    skill_dirs = _resolved_skill_dirs(
        project_root=project_root,
        builtin_names=builtin_names,
        selected_names=selected_names,
    )
    chars = 0
    for skill_dir in skill_dirs.values():
        path = skill_dir / "SKILL.md"
        try:
            chars += len(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return len(skill_dirs), chars


def seed_skills_into_workspace(
    workspace_root: Path,
    *,
    project_root: Path | None,
    builtin_names: Sequence[str],
    selected_names: Sequence[str] | None = None,
) -> list[str]:
    """Copy builtin + project skill dirs into <workspace_root>/.agents/skills/.

    Project skills override builtins of the same name. Best-effort: a failure to
    copy one dir is recorded as a warning and does not abort the rest.
    """
    warnings: list[str] = []
    dest_root = workspace_root / ".agents" / "skills"

    ordered = _resolved_skill_dirs(
        project_root=project_root,
        builtin_names=builtin_names,
        selected_names=selected_names,
    )

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
    selected_names: Sequence[str] | None = None,
) -> str:
    """Stable digest over the seeded skill set (names + file contents)."""
    h = hashlib.sha256()
    ordered = _resolved_skill_dirs(
        project_root=project_root,
        builtin_names=builtin_names,
        selected_names=selected_names,
    )

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
