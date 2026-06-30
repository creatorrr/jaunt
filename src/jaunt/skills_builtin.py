"""Registry for Jaunt's bundled (package-only) builtin Codex skills."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# The curated default set shipped with Jaunt. Kept sorted for determinism.
DEFAULT_BUILTIN_SKILLS: tuple[str, ...] = (
    "asyncpg",
    "dbos",
    "descope",
    "fastmcp",
    "openai",
    "pydantic",
    "pydantic-ai",
    "pytest",
    "ruff",
    "spacy",
    "starlette",
    "ty",
    "uv",
)


def builtin_skills_dir() -> Path:
    """Absolute path to the packaged builtin skills directory."""
    return (Path(__file__).resolve().parent / "skills" / "builtin").resolve()


def resolve_builtin_skill(name: str) -> Path | None:
    """Return the SKILL.md path for *name*, or None if it is not bundled.

    *name* must be a single safe path component. Values containing path
    separators, ``..``, or absolute paths are rejected so a hostile or
    mistyped ``[skills] builtin_skills`` entry cannot escape the bundled
    directory when the resolved path is later joined into a workspace.
    """
    from jaunt.skill_manager import validate_skill_name

    try:
        name = validate_skill_name(name)
    except ValueError:
        return None

    base = builtin_skills_dir()
    candidate = (base / name / "SKILL.md").resolve()
    # Defense in depth: never escape the bundled skills directory.
    if base not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def iter_enabled_builtin_skill_dirs(names: Iterable[str]) -> list[tuple[str, Path]]:
    """Resolve each requested builtin name to (name, skill_dir); skip missing ones."""
    pairs: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        skill_md = resolve_builtin_skill(name)
        if skill_md is not None:
            pairs.append((name, skill_md.parent))
    return pairs
