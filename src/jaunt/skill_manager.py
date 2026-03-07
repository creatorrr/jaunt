"""Core skill management: discovery, CRUD, import, and injection block building."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_SKILL_TEMPLATE = """\
# {name}

## What it is
<!-- Describe what this tool/library/API does -->

## Core concepts
<!-- Key abstractions, types, or patterns -->

## Common patterns
<!-- Code snippets or usage examples -->

## Gotchas
<!-- Pitfalls, edge cases, or common mistakes -->

## Testing notes
<!-- How to test code that uses this -->
"""


@dataclass(frozen=True, slots=True)
class SkillInfo:
    name: str
    path: Path
    source: Literal["auto", "user"]
    dist: str | None
    version: str | None


def skills_dir(project_root: Path) -> Path:
    return project_root / ".agents" / "skills"


def validate_skill_name(name: str) -> str:
    """Validate and normalize a skill name. Raises ValueError on invalid input."""
    name = name.strip()
    if not name:
        raise ValueError("Skill name must not be empty.")
    for bad in ("/", "\\", "..", "\x00"):
        if bad in name:
            raise ValueError(f"Skill name must not contain {bad!r}: {name!r}")
    if name != Path(name).name:
        raise ValueError(f"Skill name must be a single path component: {name!r}")
    return name


def discover_all_skills(project_root: Path) -> list[SkillInfo]:
    """Glob */SKILL.md under .agents/skills/, classify auto vs user."""
    from jaunt.skills_auto import _parse_generated_header

    sd = skills_dir(project_root)
    if not sd.is_dir():
        return []

    results: list[SkillInfo] = []
    for skill_md in sorted(sd.glob("*/SKILL.md")):
        dir_name = skill_md.parent.name
        try:
            txt = skill_md.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue

        first_line = txt.splitlines()[0] if txt.strip() else ""
        header = _parse_generated_header(first_line)
        if header is not None:
            dist, version = header
            results.append(
                SkillInfo(name=dir_name, path=skill_md, source="auto", dist=dist, version=version)
            )
        else:
            results.append(
                SkillInfo(name=dir_name, path=skill_md, source="user", dist=None, version=None)
            )

    results.sort(key=lambda s: s.name.lower())
    return results


def build_skills_block(project_root: Path, *, pypi_dists: dict[str, str] | None = None) -> str:
    """Build injection block from ALL skills on disk (auto + user).

    Returns a deterministic string suitable for LLM prompt injection.
    """
    from jaunt.skills_auto import _parse_generated_header

    sd = skills_dir(project_root)
    if not sd.is_dir():
        return ""

    from jaunt.external_imports import pep503_normalize

    # When pypi_dists is provided, only include auto skills whose dist is
    # still actively imported.  User skills are always included.
    active_dists: set[str] | None = None
    if pypi_dists is not None:
        active_dists = {pep503_normalize(d) for d in pypi_dists}

    sections: list[str] = []

    # Collect all SKILL.md files, sorted by directory name for determinism.
    for skill_md in sorted(sd.glob("*/SKILL.md"), key=lambda p: p.parent.name.lower()):
        dir_name = skill_md.parent.name
        try:
            txt = skill_md.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue

        lines = txt.splitlines()
        first_line = lines[0] if lines else ""
        header = _parse_generated_header(first_line)

        if header is not None:
            dist, version = header
            # Skip auto skills for distributions no longer imported.
            if active_dists is not None and pep503_normalize(dist) not in active_dists:
                continue
            body = "\n".join(lines[1:]).lstrip("\n")
            heading = f"{dist}=={version}"
        else:
            body = txt
            heading = dir_name

        body = (body or "").strip()
        if not body:
            continue
        sections.append(f"## {heading}\n{body}\n")

    return "\n".join(sections).strip()


def add_skill(project_root: Path, name: str) -> Path:
    """Create a new user skill from template. Raises FileExistsError if exists."""
    name = validate_skill_name(name)
    path = skills_dir(project_root) / name / "SKILL.md"
    if path.exists():
        raise FileExistsError(f"Skill already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SKILL_TEMPLATE.format(name=name), encoding="utf-8")
    return path


def remove_skill(project_root: Path, name: str) -> Path:
    """Remove a skill directory. Raises FileNotFoundError if missing."""
    name = validate_skill_name(name)
    skill_path = skills_dir(project_root) / name
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill not found: {skill_path}")
    shutil.rmtree(skill_path)
    return skill_path


def show_skill(project_root: Path, name: str) -> str:
    """Read and return SKILL.md content."""
    name = validate_skill_name(name)
    path = skills_dir(project_root) / name / "SKILL.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill not found: {path}")
    return path.read_text(encoding="utf-8")


def remove_auto_skills(project_root: Path) -> list[str]:
    """Remove all auto-generated skill dirs. Returns list of removed names."""
    removed: list[str] = []
    for info in discover_all_skills(project_root):
        if info.source == "auto":
            shutil.rmtree(info.path.parent)
            removed.append(info.name)
    return removed


def _git_toplevel(start: Path) -> Path | None:
    """Best-effort git root detection."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=str(start),
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    return None


def find_importable_skills(
    project_root: Path, *, from_dir: Path | None = None
) -> list[tuple[str, Path]]:
    """Find skills importable from ancestor .claude/skills/ or .codex/skills/ dirs."""
    if from_dir is not None:
        results: list[tuple[str, Path]] = []
        if from_dir.is_dir():
            for skill_md in sorted(from_dir.glob("*/SKILL.md")):
                results.append((skill_md.parent.name, skill_md))
        return results

    git_root = _git_toplevel(project_root)
    # Walk up from project_root to git root (max 3 levels if no git root).
    ancestors: list[Path] = []
    cur = project_root.resolve()
    limit = git_root.resolve() if git_root else None
    max_levels = 20 if limit else 3

    for _ in range(max_levels + 1):
        ancestors.append(cur)
        if limit and cur == limit:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent

    seen_names: set[str] = set()
    results = []
    for ancestor in ancestors:
        for subdir in (".claude/skills", ".codex/skills"):
            search_dir = ancestor / subdir
            if not search_dir.is_dir():
                continue
            for skill_md in sorted(search_dir.glob("*/SKILL.md")):
                name = skill_md.parent.name
                if name not in seen_names:
                    seen_names.add(name)
                    results.append((name, skill_md))

    return results


def import_skills(
    project_root: Path, *, from_dir: Path | None = None, dry_run: bool = False
) -> list[tuple[str, Path, str]]:
    """Import skills from external dirs into .agents/skills/. Returns (name, source, status)."""
    importable = find_importable_skills(project_root, from_dir=from_dir)
    sd = skills_dir(project_root)
    results: list[tuple[str, Path, str]] = []

    for name, source_path in importable:
        dest_dir = sd / name
        if dest_dir.exists():
            results.append((name, source_path, "skipped"))
        elif dry_run:
            results.append((name, source_path, "imported"))
        else:
            # Copy the entire skill directory (SKILL.md + sibling files like references/, assets/).
            shutil.copytree(source_path.parent, dest_dir)
            results.append((name, source_path, "imported"))

    return results
