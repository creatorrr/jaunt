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
    """Return the SKILL.md path for *name*, or None if it is not bundled."""
    candidate = builtin_skills_dir() / name / "SKILL.md"
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
