from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class BuiltinAgentEvalStep:
    name: str
    kind: Literal["jaunt", "python"]
    args: tuple[str, ...] = ()
    code: str = ""


@dataclass(frozen=True, slots=True)
class BuiltinAgentEvalCase:
    case_id: str
    description: str
    files: dict[str, str]
    steps: tuple[BuiltinAgentEvalStep, ...]
    required_packages: tuple[str, ...] = ()


def get_builtin_agent_eval_cases() -> list[BuiltinAgentEvalCase]:
    return [
        BuiltinAgentEvalCase(
            case_id="aider_multimodule_build_test",
            description="Aider-backed multi-module build plus generated tests.",
            files={
                "src/app/__init__.py": "",
                "src/app/base_specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic()
def normalize_title(raw: str) -> str:
    """
    Normalize a title for slug creation.

    Rules:
    - strip surrounding whitespace
    - lowercase
    - collapse internal whitespace to single spaces
    - raise ValueError if empty after normalization
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
                "src/app/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic(deps="app.base_specs:normalize_title")
def make_slug(title: str) -> str:
    """
    Create a slug from a title using normalize_title(title).

    Rules:
    - Replace spaces with '-'.
    - Keep only lowercase letters, digits, and '-'.
    - Collapse duplicate '-' characters.
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
                "tests/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.test(deps="app.specs:make_slug")
def test_make_slug() -> None:
    """
    Verify make_slug handles normal input and punctuation cleanup.
    """
    raise RuntimeError("test spec stub")
''',
            },
            steps=(
                BuiltinAgentEvalStep(
                    name="build",
                    kind="jaunt",
                    args=("build", "--force", "--no-progress"),
                ),
                BuiltinAgentEvalStep(
                    name="test",
                    kind="jaunt",
                    args=("test", "--no-build", "--force", "--no-progress"),
                ),
            ),
        ),
        BuiltinAgentEvalCase(
            case_id="aider_skill_build_then_codegen",
            description="User-managed skill build followed by normal Aider build/test.",
            files={
                ".agents/skills/rich/SKILL.md": "# rich\n\n## What it is\n\nTODO\n",
                ".agents/skills/rich/META.json": (
                    '{\n  "name": "rich",\n  "source": "user",\n  "dist": "rich"\n}\n'
                ),
                "src/app/__init__.py": "",
                "src/app/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic()
def title_case_words(raw: str) -> str:
    """
    Return title-cased words from an input string.

    Rules:
    - Trim surrounding whitespace.
    - Collapse repeated internal whitespace to single spaces.
    - Title-case each word.
    - Raise ValueError for empty input after trimming.
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
                "tests/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.test(deps="app.specs:title_case_words")
def test_title_case_words() -> None:
    """
    Verify normal title casing and empty-input failure behavior.
    """
    raise RuntimeError("test spec stub")
''',
            },
            steps=(
                BuiltinAgentEvalStep(
                    name="skill_build",
                    kind="jaunt",
                    args=("skill", "build", "rich"),
                ),
                BuiltinAgentEvalStep(
                    name="build",
                    kind="jaunt",
                    args=("build", "--force", "--no-progress"),
                ),
                BuiltinAgentEvalStep(
                    name="test",
                    kind="jaunt",
                    args=("test", "--no-build", "--force", "--no-progress"),
                ),
            ),
            required_packages=("rich",),
        ),
        BuiltinAgentEvalCase(
            case_id="aider_skill_refresh_then_codegen",
            description="Auto skill refresh for an external library plus Aider build/test.",
            files={
                "src/app/__init__.py": "",
                "src/app/specs.py": '''from __future__ import annotations

from typing import Any

import jaunt


@jaunt.magic(
    prompt=(
        "Use pydantic BaseModel with strict validation (no string-to-int coercion). "
        "Generated code should raise ValueError on validation failure."
    )
)
def parse_user(payload: dict[str, Any]) -> tuple[str, int]:
    """
    Parse and validate a user payload with pydantic.

    Input payload keys:
    - name: non-empty string
    - age: integer >= 0
    - Do not coerce types: values like {"age": "31"} are invalid.

    Return: (name, age)
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
                "tests/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.test(deps="app.specs:parse_user")
def test_parse_user() -> None:
    """
    Verify valid input succeeds and string age input is rejected.
    """
    raise RuntimeError("test spec stub")
''',
            },
            steps=(
                BuiltinAgentEvalStep(
                    name="skill_refresh",
                    kind="jaunt",
                    args=("skill", "refresh"),
                ),
                BuiltinAgentEvalStep(
                    name="assert_skill_file",
                    kind="python",
                    code="""from pathlib import Path

skill = Path(".agents/skills/pydantic/SKILL.md")
assert skill.is_file(), "expected pydantic auto-skill"
header = skill.read_text(encoding="utf-8").splitlines()[0]
assert "jaunt:skill=pypi" in header
""",
                ),
                BuiltinAgentEvalStep(
                    name="build",
                    kind="jaunt",
                    args=("build", "--force", "--no-progress"),
                ),
                BuiltinAgentEvalStep(
                    name="test",
                    kind="jaunt",
                    args=("test", "--no-build", "--force", "--no-progress"),
                ),
            ),
            required_packages=("pydantic",),
        ),
    ]
