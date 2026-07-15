"""Deterministic Agent-Skills for direct TypeScript package dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jaunt.skill_manager import _atomic_write_text
from jaunt.typescript.config import TypeScriptTargetConfig

# Runtime-facing direct dependencies only. Test/build tooling in devDependencies
# already has purpose-built Jaunt guidance and must not restale generated behavior
# merely because Vitest, TypeScript, or @usejaunt/ts itself changes.
_DEPENDENCY_TABLES = ("dependencies", "peerDependencies", "optionalDependencies")
_GENERATED_MARKER = "x-jaunt-npm-package"


@dataclass(frozen=True, slots=True)
class NpmSkillsResult:
    generated: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    planned_file_count: int = 0
    planned_total_bytes: int = 0

    def metadata(self) -> dict[str, Any]:
        """Return report metadata only when a direct dependency was observed."""

        values: dict[str, Any] = {
            "generated": self.generated,
            "skipped": self.skipped,
            "removed": self.removed,
            "warnings": self.warnings,
            "plan": {
                "file_count": self.planned_file_count,
                "total_bytes": self.planned_total_bytes,
            },
        }
        return (
            values
            if self.planned_file_count or any(values[key] for key in values if key != "plan")
            else {}
        )


@dataclass(frozen=True, slots=True)
class NpmSkillsPlan:
    """Read-only preview of the npm skill surface before any files are written."""

    file_count: int = 0
    total_bytes: int = 0
    packages: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "plan": {
                "file_count": self.file_count,
                "total_bytes": self.total_bytes,
                "packages": self.packages,
            },
            **({"warnings": self.warnings} if self.warnings else {}),
        }


@dataclass(frozen=True, slots=True)
class _PlannedSkill:
    package: str
    version: str
    skill_name: str
    content: str


def _skill_name(package: str) -> str:
    normalized = package.lower().replace("@", "").replace("/", "-")
    normalized = re.sub(r"[^a-z0-9-]+", "-", normalized).strip("-")
    return f"npm-{normalized}" or "npm-package"


def _skill_names(packages: tuple[str, ...]) -> dict[str, str]:
    """Return collision-free names independent of package traversal order."""

    grouped: dict[str, list[str]] = {}
    for package in packages:
        grouped.setdefault(_skill_name(package), []).append(package)
    result: dict[str, str] = {}
    for base, names in grouped.items():
        if len(names) == 1:
            result[names[0]] = base
            continue
        for package in sorted(names):
            suffix = hashlib.sha256(package.encode("utf-8")).hexdigest()[:8]
            result[package] = f"{base}-{suffix}"
    return result


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else None


def _nearest_package_owner(root: Path, start: Path) -> Path | None:
    current = start.resolve()
    root = root.resolve()
    if current.is_file() or current.suffix:
        current = current.parent
    while current == root or current.is_relative_to(root):
        if (current / "package.json").is_file():
            return current
        if current == root:
            break
        current = current.parent
    return None


def typescript_package_owners(root: Path, target: TypeScriptTargetConfig) -> tuple[Path, ...]:
    """Resolve package owners without starting the analyzer worker.

    This runs before worker initialization so newly materialized skills participate
    in the exact generation fingerprint embedded in the analyzer session.
    """

    root = root.resolve()
    candidates: list[Path] = []
    for raw in (*target.projects, *target.test_projects):
        matches = sorted(root.glob(raw)) if any(char in raw for char in "*?[") else [root / raw]
        candidates.extend(matches)
    candidates.append(root / target.tool_owner)
    owners = {
        owner
        for candidate in candidates
        if (owner := _nearest_package_owner(root, candidate)) is not None
    }
    return tuple(sorted(owners, key=lambda path: path.as_posix()))


def _direct_dependencies(owner: Path) -> tuple[str, ...]:
    manifest = _read_manifest(owner / "package.json")
    if manifest is None:
        return ()
    names: set[str] = set()
    for table_name in _DEPENDENCY_TABLES:
        table = manifest.get(table_name)
        if isinstance(table, dict):
            names.update(str(name) for name in table if isinstance(name, str) and name.strip())
    return tuple(sorted(names))


def _installed_package(root: Path, owner: Path, package: str) -> tuple[Path, dict[str, Any]] | None:
    relative = Path(*package.split("/"))
    current = owner.resolve()
    root = root.resolve()
    while current == root or current.is_relative_to(root):
        package_root = current / "node_modules" / relative
        manifest = _read_manifest(package_root / "package.json")
        if manifest is not None:
            return package_root, manifest
        if current == root:
            break
        current = current.parent
    return None


def _readme_excerpt(package_root: Path, max_chars: int) -> str:
    candidates = sorted(
        path
        for path in package_root.iterdir()
        if path.is_file() and path.name.lower() in {"readme", "readme.md", "readme.markdown"}
    )
    if not candidates:
        return "No installed package README was available. Consult the package's own documentation."
    try:
        text = candidates[0].read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return (
            "The installed package README could not be read. Consult the package's documentation."
        )
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n\n[README excerpt truncated by Jaunt]"
    return text


def _render_skill(
    package: str,
    version: str,
    description: str,
    readme: str,
    *,
    skill_name: str,
) -> str:
    summary = description.strip() or f"The installed {package} npm package."
    return (
        "---\n"
        f"name: {json.dumps(skill_name)}\n"
        f"description: {json.dumps(f'Use when generating TypeScript code with {package}.')}\n"
        f"{_GENERATED_MARKER}: {json.dumps(package)}\n"
        f"x-jaunt-npm-version: {json.dumps(version)}\n"
        "---\n"
        "## What it is\n\n"
        f"{summary}\n\n"
        "## Core concepts\n\n"
        f"This skill reflects direct dependency `{package}` at installed version `{version}`. "
        "Prefer its public exports and the consuming project's configured module system.\n\n"
        "## Common patterns\n\n"
        "Treat the installed README below as reference material, then verify APIs against the "
        "project's compiler and tests.\n\n"
        "### Installed README excerpt\n\n"
        f"{readme}\n\n"
        "## Gotchas\n\n"
        "Do not import undeclared transitive packages or internal package paths that are absent "
        "from the package exports map. Keep runtime imports separate from type-only imports.\n\n"
        "## Testing notes\n\n"
        "Run the owning TypeScript project's typecheck and its configured test runner after "
        "using this package.\n"
    )


def _generated_metadata(path: Path) -> tuple[str, str] | None:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not source.startswith("---\n"):
        return None
    frontmatter_end = source.find("\n---", 4)
    if frontmatter_end < 0:
        return None
    frontmatter = source[4:frontmatter_end]
    package_match = re.search(
        rf"^{re.escape(_GENERATED_MARKER)}:\s*[\"']?([^\"'\n]+)", frontmatter, re.M
    )
    version_match = re.search(r"^x-jaunt-npm-version:\s*[\"']?([^\"'\n]+)", frontmatter, re.M)
    if package_match is None or version_match is None:
        return None
    return package_match.group(1).strip(), version_match.group(1).strip()


def _filesystem_warning(skill_name: str, action: str) -> str:
    """Describe an optional skill update failure without platform-specific errno text."""

    return f"optional npm skill {skill_name!r} not {action}: filesystem error"


def _planned_skills(
    project_root: Path,
    package_owners: tuple[Path, ...],
    max_readme_chars: int,
) -> tuple[tuple[_PlannedSkill, ...], tuple[str, ...], dict[str, str]]:
    packages = sorted({name for owner in package_owners for name in _direct_dependencies(owner)})
    skill_names = _skill_names(tuple(packages))
    planned: list[_PlannedSkill] = []
    warnings: list[str] = []
    for package in packages:
        installed = next(
            (
                resolved
                for owner in package_owners
                if (resolved := _installed_package(project_root, owner, package)) is not None
            ),
            None,
        )
        if installed is None:
            warnings.append(
                f"direct npm dependency {package!r} is not installed; skill not generated"
            )
            continue
        package_root, manifest = installed
        version = str(manifest.get("version", "unknown"))
        skill_name = skill_names[package]
        planned.append(
            _PlannedSkill(
                package=package,
                version=version,
                skill_name=skill_name,
                content=_render_skill(
                    package,
                    version,
                    str(manifest.get("description", "")),
                    _readme_excerpt(package_root, max(512, int(max_readme_chars))),
                    skill_name=skill_name,
                ),
            )
        )
    return tuple(planned), tuple(warnings), skill_names


def plan_npm_skills(
    *,
    project_root: Path,
    package_owners: tuple[Path, ...],
    max_readme_chars: int = 8_000,
) -> NpmSkillsPlan:
    """Preview the complete direct-dependency skill footprint without writing files."""

    project_root = project_root.resolve()
    planned, warnings, _skill_names_by_package = _planned_skills(
        project_root,
        package_owners,
        max_readme_chars,
    )
    return NpmSkillsPlan(
        file_count=len(planned),
        total_bytes=sum(len(item.content.encode("utf-8")) for item in planned),
        packages=tuple(item.package for item in planned),
        warnings=warnings,
    )


def _remove_stale_managed_skills(
    project_root: Path,
    desired_names: dict[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Remove npm-managed SKILL.md files outside the current desired mapping."""

    skills_root = project_root / ".agents" / "skills"
    if not skills_root.is_dir():
        return (), ()
    removed: list[str] = []
    warnings: list[str] = []
    desired_paths = {
        package: (skills_root / skill_name / "SKILL.md").resolve()
        for package, skill_name in desired_names.items()
    }
    for path in sorted(skills_root.glob("*/SKILL.md")):
        metadata = _generated_metadata(path)
        if metadata is None:
            continue
        package, _version = metadata
        if desired_paths.get(package) == path.resolve():
            continue
        skill_name = path.parent.name
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            warnings.append(_filesystem_warning(skill_name, "removed"))
            continue
        removed.append(skill_name)
        try:
            path.parent.rmdir()
        except OSError:
            # Preserve any user/resource files sharing the directory. Without a
            # SKILL.md, Codex no longer discovers the stale managed skill.
            pass
    return tuple(removed), tuple(warnings)


def ensure_npm_skills(
    *,
    project_root: Path,
    package_owners: tuple[Path, ...],
    max_readme_chars: int = 8_000,
) -> NpmSkillsResult:
    """Create/update skills for installed direct dependencies only.

    Lockfile-only and transitive dependencies are intentionally ignored. Existing
    user-authored skills are never overwritten; Jaunt updates only files carrying
    its npm provenance fields.
    """

    project_root = project_root.resolve()
    generated: list[str] = []
    skipped: list[str] = []
    planned, plan_warnings, skill_names = _planned_skills(
        project_root,
        package_owners,
        max_readme_chars,
    )
    warnings: list[str] = list(plan_warnings)
    removed, removal_warnings = _remove_stale_managed_skills(project_root, skill_names)
    warnings.extend(removal_warnings)
    for item in planned:
        package = item.package
        version = item.version
        skill_name = item.skill_name
        path = project_root / ".agents" / "skills" / skill_name / "SKILL.md"
        existing = _generated_metadata(path) if path.exists() else None
        if path.exists() and existing is None:
            skipped.append(skill_name)
            continue
        if existing == (package, version):
            skipped.append(skill_name)
            continue
        try:
            _atomic_write_text(path, item.content)
        except OSError:
            warnings.append(_filesystem_warning(skill_name, "written"))
            continue
        generated.append(skill_name)
    return NpmSkillsResult(
        generated=tuple(generated),
        skipped=tuple(skipped),
        removed=removed,
        warnings=tuple(warnings),
        planned_file_count=len(planned),
        planned_total_bytes=sum(len(item.content.encode("utf-8")) for item in planned),
    )


__all__ = [
    "NpmSkillsResult",
    "NpmSkillsPlan",
    "ensure_npm_skills",
    "plan_npm_skills",
    "typescript_package_owners",
]
