"""Core skill management: discovery, CRUD, and import."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from jaunt.lib_inspect import LibRef

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

_BOOTSTRAPPED_TEMPLATE = """\
# {name}

## What it is
{description}

## Libraries
{lib_info}

## Module structure
{module_structure}

## Public API
{public_api}

## Core concepts
<!-- Fill in after running `jaunt skill build {name}` -->

## Common patterns
<!-- Fill in after running `jaunt skill build {name}` -->

## Gotchas
<!-- Fill in after running `jaunt skill build {name}` -->

## Testing notes
<!-- Fill in after running `jaunt skill build {name}` -->
"""


@dataclass(frozen=True, slots=True)
class SkillMeta:
    libs: list[dict[str, str | None]] = field(default_factory=list)
    description: str | None = None


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text to a file atomically via temp file + os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".jaunt-tmp-",
        suffix=".md",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


@dataclass(frozen=True, slots=True)
class SkillInfo:
    name: str
    path: Path
    source: Literal["auto", "user"]
    dist: str | None
    version: str | None
    registry: Literal["user", "managed", "classic"] = "user"
    managed_kind: Literal["pypi", "npm"] | None = None


def skills_dir(project_root: Path) -> Path:
    return project_root / ".agents" / "skills"


def managed_skills_dir(project_root: Path) -> Path:
    """Return the tracked project-local registry for Jaunt-managed skills."""

    return project_root / ".jaunt" / "skills"


def parse_managed_skill_meta(
    text: str,
) -> tuple[Literal["pypi", "npm"], str, str] | None:
    """Return managed skill provenance from Agent-Skills frontmatter."""

    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        metadata[key.strip()] = value.strip().strip("\"'")
    dist = metadata.get("x-jaunt-dist")
    dist_version = metadata.get("x-jaunt-version")
    if dist and dist_version:
        return "pypi", dist, dist_version
    package = metadata.get("x-jaunt-npm-package")
    package_version = metadata.get("x-jaunt-npm-version")
    if package and package_version:
        return "npm", package, package_version
    return None


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
    """Discover user, managed, and not-yet-migrated managed skills."""
    results: list[SkillInfo] = []
    roots = ((managed_skills_dir(project_root), "managed"), (skills_dir(project_root), "user"))
    for root, registry in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            try:
                text = skill_md.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            metadata = parse_managed_skill_meta(text)
            if metadata is not None:
                kind, package, version = metadata
                results.append(
                    SkillInfo(
                        name=skill_md.parent.name,
                        path=skill_md,
                        source="auto",
                        dist=package,
                        version=version,
                        registry="managed" if registry == "managed" else "classic",
                        managed_kind=kind,
                    )
                )
            elif registry == "user":
                results.append(
                    SkillInfo(
                        name=skill_md.parent.name,
                        path=skill_md,
                        source="user",
                        dist=None,
                        version=None,
                        registry="user",
                    )
                )

    results.sort(key=lambda s: s.name.lower())
    return results


def read_skill_meta(project_root: Path, name: str) -> SkillMeta | None:
    """Read .agents/skills/<name>/META.json. Returns None if missing."""
    name = validate_skill_name(name)
    meta_path = skills_dir(project_root) / name / "META.json"
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return SkillMeta(
            libs=data.get("libs", []),
            description=data.get("description"),
        )
    except Exception:  # noqa: BLE001
        return None


def write_skill_meta(project_root: Path, name: str, meta: SkillMeta) -> Path:
    """Write META.json alongside SKILL.md."""
    name = validate_skill_name(name)
    meta_path = skills_dir(project_root) / name / "META.json"
    data = {
        "libs": meta.libs,
        "description": meta.description,
    }
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(meta_path, content)
    return meta_path


def add_skill(
    project_root: Path,
    name: str,
    *,
    description: str | None = None,
    libs: list[LibRef] | None = None,
) -> Path:
    """Create a new user skill from template. Raises FileExistsError if exists."""
    name = validate_skill_name(name)
    path = skills_dir(project_root) / name / "SKILL.md"
    if path.exists():
        raise FileExistsError(f"Skill already exists: {path}")

    if libs:
        from jaunt.lib_inspect import inspect_lib

        lib_contents = [inspect_lib(ref) for ref in libs]

        # Build template sections
        desc = description or " / ".join(lc.summary for lc in lib_contents if lc.summary) or name
        lib_info_parts = []
        module_parts = []
        api_parts = []
        for lc in lib_contents:
            ver = lc.version or "unknown"
            summary = lc.summary or ""
            lib_info_parts.append(f"- {lc.ref.name}=={ver} — {summary}")
            if lc.module_structure:
                module_parts.append(lc.module_structure)
            if lc.public_api:
                api_parts.append(lc.public_api)

        content = _BOOTSTRAPPED_TEMPLATE.format(
            name=name,
            description=desc,
            lib_info="\n".join(lib_info_parts) or "None",
            module_structure="\n".join(module_parts) or "None",
            public_api="\n".join(api_parts) or "None",
        )

        _atomic_write_text(path, content)

        # Write META.json — store local paths relative to project_root for portability
        lib_dicts = []
        for ref in libs:
            stored_path = ref.path
            if stored_path is not None:
                try:
                    stored_path = str(
                        Path(stored_path).resolve().relative_to(project_root.resolve())
                    )
                except ValueError:
                    pass  # outside project root — store absolute as fallback
            lib_dicts.append(
                {"type": ref.type, "name": ref.name, "path": stored_path, "version": ref.version}
            )
        write_skill_meta(project_root, name, SkillMeta(libs=lib_dicts, description=description))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = _SKILL_TEMPLATE.format(name=name)
        if description:
            content = content.replace(
                "<!-- Describe what this tool/library/API does -->",
                description,
            )
        path.write_text(content, encoding="utf-8")

    return path


def remove_skill(project_root: Path, name: str) -> Path:
    """Remove a user-owned skill directory. Managed skills use refresh/migrate."""
    name = validate_skill_name(name)
    skill_path = skills_dir(project_root) / name
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill not found: {skill_path}")
    skill_md = skill_path / "SKILL.md"
    if skill_md.is_file():
        try:
            managed = parse_managed_skill_meta(skill_md.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            managed = None
        if managed is not None:
            raise ValueError(
                f"Skill '{name}' is Jaunt-managed; run `jaunt skill migrate` or refresh skills"
            )
    shutil.rmtree(skill_path)
    return skill_path


def show_skill(project_root: Path, name: str) -> str:
    """Read and return SKILL.md content."""
    name = validate_skill_name(name)
    user_path = skills_dir(project_root) / name / "SKILL.md"
    if user_path.exists():
        user_text = user_path.read_text(encoding="utf-8")
        if parse_managed_skill_meta(user_text) is None:
            return user_text
    managed_path = managed_skills_dir(project_root) / name / "SKILL.md"
    if managed_path.exists():
        return managed_path.read_text(encoding="utf-8")
    if user_path.exists():
        return user_text
    raise FileNotFoundError(f"Skill not found: {user_path}")


def remove_auto_skills(project_root: Path, *, include_classic: bool = True) -> list[str]:
    """Remove managed-registry skills, optionally including classic entries."""
    removed: list[str] = []
    for info in discover_all_skills(project_root):
        if info.source == "auto" and (include_classic or info.registry == "managed"):
            shutil.rmtree(info.path.parent)
            removed.append(info.name)
    return removed


@dataclass(frozen=True, slots=True)
class SkillMigrationAction:
    name: str
    kind: Literal["pypi", "npm"]
    source: Path
    destination: Path
    action: Literal["move", "deduplicate"]


@dataclass(frozen=True, slots=True)
class SkillMigrationPlan:
    actions: tuple[SkillMigrationAction, ...] = ()
    conflicts: tuple[str, ...] = ()


def _directory_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"symlinked managed skill content is unsafe: {item}")
        if not item.is_file():
            continue
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def plan_managed_skill_migration(project_root: Path) -> SkillMigrationPlan:
    """Plan classic ``.agents/skills`` managed entries into ``.jaunt/skills``."""

    actions: list[SkillMigrationAction] = []
    conflicts: list[str] = []
    source_root = skills_dir(project_root)
    if not source_root.is_dir():
        return SkillMigrationPlan()
    for skill_md in sorted(source_root.glob("*/SKILL.md")):
        source = skill_md.parent
        if source.is_symlink():
            conflicts.append(f"{source}: symlinked skill directories are not migrated")
            continue
        try:
            metadata = parse_managed_skill_meta(skill_md.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            conflicts.append(f"{skill_md}: unreadable managed skill: {exc}")
            continue
        if metadata is None:
            continue
        kind, _package, _version = metadata
        try:
            source_digest = _directory_digest(source)
        except (OSError, ValueError) as exc:
            conflicts.append(f"{source.name}: unsafe managed skill: {exc}")
            continue
        destination = managed_skills_dir(project_root) / source.name
        if not destination.exists():
            actions.append(SkillMigrationAction(source.name, kind, source, destination, "move"))
            continue
        try:
            identical = source_digest == _directory_digest(destination)
        except (OSError, ValueError) as exc:
            conflicts.append(f"{source.name}: cannot compare destination: {exc}")
            continue
        if identical:
            actions.append(
                SkillMigrationAction(source.name, kind, source, destination, "deduplicate")
            )
        else:
            conflicts.append(
                f"{source.name}: destination already exists with different contents: {destination}"
            )
    return SkillMigrationPlan(tuple(actions), tuple(conflicts))


_TRACKED_SKILLS_GITIGNORE_BLOCK = """\
# Jaunt local state; managed skills are tracked.
!/.jaunt/
/.jaunt/*
!/.jaunt/skills/
!/.jaunt/skills/**
"""


def ensure_managed_skills_gitignore(project_root: Path) -> bool:
    """Append the canonical tracked-skills exception block when it is absent."""

    path = project_root / ".gitignore"
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if _TRACKED_SKILLS_GITIGNORE_BLOCK.strip() in original:
        return False
    joiner = "" if not original or original.endswith("\n") else "\n"
    content = original + joiner + _TRACKED_SKILLS_GITIGNORE_BLOCK
    _atomic_write_text(path, content)
    try:
        inside_git = (
            subprocess.run(
                ["git", "-C", str(project_root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            == 0
        )
    except FileNotFoundError:
        inside_git = False
    if inside_git:
        runtime_ignored = (
            subprocess.run(
                ["git", "-C", str(project_root), "check-ignore", "-q", ".jaunt/.runtime-probe"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        skills_ignored = (
            subprocess.run(
                ["git", "-C", str(project_root), "check-ignore", "-q", ".jaunt/skills/.probe"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if not runtime_ignored or skills_ignored:
            _atomic_write_text(path, original)
            raise ValueError(
                "could not configure .gitignore to track .jaunt/skills while ignoring runtime state"
            )
    return True


def apply_managed_skill_migration(project_root: Path, plan: SkillMigrationPlan) -> None:
    """Apply a validated, conflict-free managed skill migration."""

    if plan.conflicts:
        raise ValueError("cannot apply a managed skill migration with conflicts")
    ensure_managed_skills_gitignore(project_root)
    for action in plan.actions:
        action.destination.parent.mkdir(parents=True, exist_ok=True)
        if action.action == "move":
            shutil.move(str(action.source), str(action.destination))
        else:
            shutil.rmtree(action.source)


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
    project_root: Path,
    *,
    names: list[str] | None = None,
    from_dir: Path | None = None,
    dry_run: bool = False,
) -> list[tuple[str, Path, str]]:
    """Import skills from external dirs into .agents/skills/. Returns (name, source, status)."""
    importable = find_importable_skills(project_root, from_dir=from_dir)
    requested = [validate_skill_name(name) for name in (names or [])]
    if requested:
        importable_by_name = {name: source_path for name, source_path in importable}
        missing = [name for name in requested if name not in importable_by_name]
        if missing:
            available = ", ".join(sorted(importable_by_name)) or "(none)"
            missing_txt = ", ".join(missing)
            raise ValueError(f"Unknown importable skill(s): {missing_txt}. Available: {available}")
        selected_names = set(requested)
        importable = [
            (name, source_path) for name, source_path in importable if name in selected_names
        ]

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
