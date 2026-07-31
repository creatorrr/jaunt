"""Deterministic selection of skills exposed to one Codex generation call."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal

from jaunt.errors import JauntConfigError
from jaunt.external_imports import pep503_normalize
from jaunt.skill_manager import (
    managed_skills_dir,
    parse_managed_skill_meta,
    skills_dir,
)
from jaunt.skills_builtin import iter_enabled_builtin_skill_dirs

Language = Literal["py", "ts"]


@dataclass(frozen=True, slots=True)
class SkillSelectionEntry:
    name: str
    source: Literal["builtin", "managed", "classic", "user"]
    reason: str


@dataclass(frozen=True, slots=True)
class SkillSelection:
    entries: tuple[SkillSelectionEntry, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)

    def metadata(self) -> list[dict[str, str]]:
        return [
            {"name": entry.name, "source": entry.source, "reason": entry.reason}
            for entry in self.entries
        ]


@dataclass(frozen=True, slots=True)
class _AvailableSkill:
    name: str
    source: Literal["builtin", "managed", "classic", "user"]
    triggers: frozenset[str]
    always_relevant: bool = False


_BUILTIN_IMPORTS: dict[str, frozenset[str]] = {
    "asyncpg": frozenset({"asyncpg"}),
    "dbos": frozenset({"dbos"}),
    "descope": frozenset({"descope"}),
    "fastmcp": frozenset({"fastmcp"}),
    "openai": frozenset({"openai"}),
    "pydantic": frozenset({"pydantic"}),
    "pydantic-ai": frozenset({"pydantic-ai"}),
    "pytest": frozenset({"pytest"}),
    "spacy": frozenset({"spacy"}),
    "starlette": frozenset({"starlette"}),
}


def _manual_triggers(skill_dir: Path) -> frozenset[str]:
    path = skill_dir / "META.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return frozenset()
    libs = value.get("libs") if isinstance(value, dict) else None
    if not isinstance(libs, list):
        return frozenset()
    names = {
        pep503_normalize(str(item.get("name", "")))
        for item in libs
        if isinstance(item, dict) and item.get("name")
    }
    return frozenset(name for name in names if name)


def _available_skills(
    project_root: Path | None, builtin_names: tuple[str, ...]
) -> dict[str, _AvailableSkill]:
    available: dict[str, _AvailableSkill] = {}
    for name, _path in iter_enabled_builtin_skill_dirs(builtin_names):
        available[name] = _AvailableSkill(
            name,
            "builtin",
            _BUILTIN_IMPORTS.get(name, frozenset()),
            always_relevant=name in {"ruff", "ty"},
        )
    if project_root is None:
        return available

    user_root = skills_dir(project_root)
    # Classic managed entries are lowest-precedence project skills.
    if user_root.is_dir():
        for skill_md in sorted(user_root.glob("*/SKILL.md")):
            try:
                managed = parse_managed_skill_meta(skill_md.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if managed is not None:
                _kind, package, _version = managed
                available[skill_md.parent.name] = _AvailableSkill(
                    skill_md.parent.name,
                    "classic",
                    frozenset({pep503_normalize(package)}),
                )
    managed_root = managed_skills_dir(project_root)
    if managed_root.is_dir():
        for skill_md in sorted(managed_root.glob("*/SKILL.md")):
            try:
                managed = parse_managed_skill_meta(skill_md.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if managed is None:
                continue
            _kind, package, _version = managed
            available[skill_md.parent.name] = _AvailableSkill(
                skill_md.parent.name,
                "managed",
                frozenset({pep503_normalize(package)}),
            )
    if user_root.is_dir():
        for skill_md in sorted(user_root.glob("*/SKILL.md")):
            try:
                managed = parse_managed_skill_meta(skill_md.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if managed is not None:
                continue
            triggers = _manual_triggers(skill_md.parent)
            available[skill_md.parent.name] = _AvailableSkill(
                skill_md.parent.name,
                "user",
                triggers,
                always_relevant=not triggers,
            )
    return available


def _python_imports(texts: tuple[str, ...]) -> set[str]:
    modules: set[str] = set()
    for text in texts:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".", 1)[0])
    normalized = {pep503_normalize(module) for module in modules}
    try:
        packages = metadata.packages_distributions()
    except Exception:
        packages = {}
    for module in modules:
        normalized.update(pep503_normalize(dist) for dist in packages.get(module, ()))
    return normalized


_TS_IMPORT_RE = re.compile(
    r"(?:from\s*|import\s+(?![\w{$*])|import\s*\(|require\s*\()\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)


def _typescript_imports(texts: tuple[str, ...]) -> set[str]:
    packages: set[str] = set()
    for text in texts:
        for specifier in _TS_IMPORT_RE.findall(text):
            if specifier.startswith((".", "/", "#", "node:")):
                continue
            parts = specifier.split("/")
            package = "/".join(parts[:2]) if specifier.startswith("@") else parts[0]
            packages.add(pep503_normalize(package))
    return packages


def select_skills(
    *,
    project_root: Path | None,
    builtin_names: tuple[str, ...],
    texts: tuple[str, ...],
    language: Language,
    kind: str,
    activation: Literal["relevant", "all"] = "relevant",
    always: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> SkillSelection:
    """Resolve the effective skill set and explain every activation."""

    available = _available_skills(project_root, builtin_names)
    configured = set(always).union(exclude)
    missing = sorted(configured.difference(available))
    if missing:
        raise JauntConfigError("Unknown configured skill(s): " + ", ".join(missing))
    imports = _python_imports(texts) if language == "py" else _typescript_imports(texts)
    entries: list[SkillSelectionEntry] = []
    excluded = set(exclude)
    forced = set(always)
    for name, skill in sorted(available.items()):
        if name in excluded:
            continue
        reason: str | None = None
        if name in forced:
            reason = "configured:always"
        elif activation == "all":
            reason = "activation:all"
        elif skill.always_relevant and skill.source == "user":
            reason = "user:unscoped"
        elif skill.always_relevant and language == "py":
            reason = "python:baseline"
        elif name == "pytest" and language == "py" and "test" in kind:
            reason = "python:test"
        elif matches := sorted(skill.triggers.intersection(imports)):
            reason = "import:" + matches[0]
        if reason is not None:
            entries.append(SkillSelectionEntry(name, skill.source, reason))
    return SkillSelection(tuple(entries))


__all__ = ["SkillSelection", "SkillSelectionEntry", "select_skills"]
