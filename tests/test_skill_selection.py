from __future__ import annotations

import json
from pathlib import Path

import pytest

from jaunt.errors import JauntConfigError
from jaunt.generate.base import ModuleSpecContext
from jaunt.skill_seed import skills_fingerprint
from jaunt.skill_selection import select_module_context_skills, select_skills


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _managed(dist: str, version: str = "1") -> str:
    return f"---\nname: {dist}\nx-jaunt-dist: {dist}\nx-jaunt-version: {version}\n---\nbody\n"


def _managed_npm(package: str, version: str = "1") -> str:
    return (
        f"---\nname: {package}\nx-jaunt-npm-package: {package}\n"
        f"x-jaunt-npm-version: {version}\n---\nbody\n"
    )


def test_relevant_selection_uses_imports_baselines_and_unscoped_manual(tmp_path: Path) -> None:
    _write(tmp_path / ".jaunt/skills/pydantic/SKILL.md", _managed("pydantic"))
    _write(tmp_path / ".jaunt/skills/httpx/SKILL.md", _managed("httpx"))
    _write(tmp_path / ".agents/skills/conventions/SKILL.md", "# conventions\n")

    selection = select_skills(
        project_root=tmp_path,
        builtin_names=("ruff", "ty", "pytest"),
        texts=("from pydantic import BaseModel\n",),
        language="py",
        kind="build",
    )

    assert set(selection.names) == {"conventions", "pydantic", "ruff", "ty"}
    assert "httpx" not in selection.names
    assert "pytest" not in selection.names


def test_manual_meta_scopes_skill_and_user_overrides_managed(tmp_path: Path) -> None:
    _write(tmp_path / ".jaunt/skills/httpx/SKILL.md", _managed("httpx"))
    _write(tmp_path / ".agents/skills/httpx/SKILL.md", "# project override\n")
    _write(
        tmp_path / ".agents/skills/httpx/META.json",
        json.dumps({"libs": [{"type": "pypi", "name": "httpx"}]}),
    )

    selection = select_skills(
        project_root=tmp_path,
        builtin_names=(),
        texts=("import httpx\n",),
        language="py",
        kind="build",
    )

    assert selection.names == ("httpx",)
    assert selection.entries[0].source == "user"


def test_manual_meta_uses_import_roots_for_differently_named_library(tmp_path: Path) -> None:
    _write(tmp_path / ".agents/skills/company-utils/SKILL.md", "# company utilities\n")
    _write(
        tmp_path / ".agents/skills/company-utils/META.json",
        json.dumps(
            {
                "libs": [
                    {
                        "type": "path",
                        "name": "company-utils",
                        "path": "libs/company-utils",
                        "import_roots": ["acme_utils"],
                    }
                ]
            }
        ),
    )

    selection = select_skills(
        project_root=tmp_path,
        builtin_names=(),
        texts=("import acme_utils\n",),
        language="py",
        kind="build",
    )

    assert selection.names == ("company-utils",)
    assert selection.entries[0].reason == "import:acme-utils"


def test_all_always_exclude_and_unknown_validation(tmp_path: Path) -> None:
    _write(tmp_path / ".agents/skills/a/SKILL.md", "a\n")
    _write(tmp_path / ".agents/skills/b/SKILL.md", "b\n")
    selection = select_skills(
        project_root=tmp_path,
        builtin_names=(),
        texts=(),
        language="py",
        kind="build",
        activation="all",
        exclude=("b",),
    )
    assert selection.names == ("a",)

    with pytest.raises(JauntConfigError, match="Unknown configured skill"):
        select_skills(
            project_root=tmp_path,
            builtin_names=(),
            texts=(),
            language="py",
            kind="build",
            always=("missing",),
        )


def test_unselected_skill_bytes_do_not_change_selected_fingerprint(tmp_path: Path) -> None:
    _write(tmp_path / ".jaunt/skills/pydantic/SKILL.md", _managed("pydantic"))
    _write(tmp_path / ".jaunt/skills/httpx/SKILL.md", _managed("httpx"))
    selection = select_skills(
        project_root=tmp_path,
        builtin_names=(),
        texts=("import pydantic\n",),
        language="py",
        kind="build",
    )
    before = skills_fingerprint(
        project_root=tmp_path,
        builtin_names=(),
        selected_names=selection.names,
    )
    _write(tmp_path / ".jaunt/skills/httpx/SKILL.md", _managed("httpx", "2"))
    after = skills_fingerprint(
        project_root=tmp_path,
        builtin_names=(),
        selected_names=selection.names,
    )
    assert before == after


def test_typescript_side_effect_import_selects_managed_skill(tmp_path: Path) -> None:
    _write(
        tmp_path / ".jaunt/skills/reflect-metadata/SKILL.md",
        _managed_npm("reflect-metadata"),
    )

    selection = select_skills(
        project_root=tmp_path,
        builtin_names=(),
        texts=('import "./setup";\nimport "reflect-metadata";\n',),
        language="ts",
        kind="build",
    )

    assert selection.names == ("reflect-metadata",)
    assert selection.entries[0].reason == "import:reflect-metadata"


def test_module_context_selection_includes_retrieved_file_bodies(tmp_path: Path) -> None:
    _write(tmp_path / ".jaunt/skills/httpx/SKILL.md", _managed("httpx"))
    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["fetch"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
        project_root=tmp_path,
        relevant_context_files=(("relevant_0.py", "import httpx\n"),),
    )

    selection = select_module_context_skills(ctx)

    assert selection.names == ("httpx",)
    assert selection.entries[0].reason == "import:httpx"
