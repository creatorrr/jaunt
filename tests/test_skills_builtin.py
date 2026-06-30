from __future__ import annotations

import re

from jaunt.skills_builtin import (
    DEFAULT_BUILTIN_SKILLS,
    builtin_skills_dir,
    iter_enabled_builtin_skill_dirs,
    resolve_builtin_skill,
)

_FRONTMATTER_RE = re.compile(r"^---\n(?P<fm>.*?)\n---\n", re.DOTALL)
_TOOLING = ("ruff", "ty", "pytest", "uv")


def test_default_set_has_13_names() -> None:
    assert len(DEFAULT_BUILTIN_SKILLS) == 13
    assert DEFAULT_BUILTIN_SKILLS == tuple(sorted(DEFAULT_BUILTIN_SKILLS))
    for name in ("asyncpg", "pydantic-ai", "ruff", "ty", "pytest", "uv"):
        assert name in DEFAULT_BUILTIN_SKILLS


def test_builtin_dir_exists() -> None:
    assert builtin_skills_dir().is_dir()


def test_tooling_skills_resolve_and_have_frontmatter() -> None:
    for name in _TOOLING:
        path = resolve_builtin_skill(name)
        assert path is not None and path.is_file(), name
        text = path.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        assert m, f"{name} missing frontmatter"
        fm = m.group("fm")
        assert re.search(rf"^name:\s*\"?{re.escape(name)}\"?\s*$", fm, re.MULTILINE), name
        assert re.search(r"^description:\s*\S", fm, re.MULTILINE), name


def test_iter_enabled_skips_unknown() -> None:
    pairs = iter_enabled_builtin_skill_dirs(["ruff", "does-not-exist"])
    names = [n for n, _ in pairs]
    assert names == ["ruff"]
