from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BuiltinEvalCase:
    case_id: str
    description: str
    files: dict[str, str]
    assertion_code: str
    required_packages: tuple[str, ...] = ()


def get_builtin_eval_cases() -> list[BuiltinEvalCase]:
    """Return the built-in eval suite definitions."""

    return [
        BuiltinEvalCase(
            case_id="simple_function",
            description="Simple function (pure logic, no deps).",
            files={
                "src/app/__init__.py": "",
                "src/app/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic()
def add(a: int, b: int) -> int:
    """
    Return the sum of two integers.

    Requirements:
    - Works for positive, negative, and zero integers.
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
            },
            assertion_code='''from app.specs import add

assert add(2, 3) == 5
assert add(-2, 3) == 1
assert add(0, 0) == 0
''',
        ),
        BuiltinEvalCase(
            case_id="complex_return_type",
            description="Function with type hints and complex return type.",
            files={
                "src/app/__init__.py": "",
                "src/app/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic()
def summarize_scores(values: list[int]) -> dict[str, int | float]:
    """
    Summarize a list of scores.

    Return a dict with keys:
    - "count": number of elements
    - "total": arithmetic sum
    - "average": arithmetic mean as float (0.0 for empty input)
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
            },
            assertion_code='''from app.specs import summarize_scores

assert summarize_scores([]) == {"count": 0, "total": 0, "average": 0.0}
assert summarize_scores([10, 20, 30]) == {"count": 3, "total": 60, "average": 20.0}
''',
        ),
        BuiltinEvalCase(
            case_id="class_methods_properties",
            description="Class with methods and properties.",
            files={
                "src/app/__init__.py": "",
                "src/app/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic()
class NameFormatter:
    """
    Format names with a configurable prefix.

    Behavior:
    - Prefix is trimmed at construction time.
    - If prefix is empty after trimming, raise ValueError.
    - `normalized_prefix` returns the lowercase prefix.
    - `format_name` returns "<normalized_prefix>:<trimmed_name>".
    - If name is empty after trimming, raise ValueError.
    """

    def __init__(self, prefix: str) -> None:
        raise RuntimeError("spec stub (generated at build time)")

    @property
    def normalized_prefix(self) -> str:
        raise RuntimeError("spec stub (generated at build time)")

    def format_name(self, name: str) -> str:
        raise RuntimeError("spec stub (generated at build time)")
''',
            },
            assertion_code='''import pytest

from app.specs import NameFormatter

fmt = NameFormatter(" Team ")
assert fmt.normalized_prefix == "team"
assert fmt.format_name("  Ada ") == "team:Ada"

with pytest.raises(ValueError):
    NameFormatter("   ")

with pytest.raises(ValueError):
    fmt.format_name("   ")
''',
        ),
        BuiltinEvalCase(
            case_id="module_with_deps",
            description="Module with dependencies on another module.",
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
            },
            assertion_code='''from app.specs import make_slug

assert make_slug("  Hello   World  ") == "hello-world"
assert make_slug("Python 3.12!!!") == "python-312"
''',
        ),
        BuiltinEvalCase(
            case_id="external_library_pydantic",
            description="Module using external library (pydantic).",
            files={
                "src/app/__init__.py": "",
                "src/app/specs.py": '''from __future__ import annotations

from typing import Any

import jaunt


@jaunt.magic(
    prompt=(
        "Use pydantic BaseModel with strict validation (no string-to-int coercion). "
        "Code must pass ty type checking: use plain str/int fields with validators; "
        "avoid constr()/conint() in type annotations."
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

    Errors:
    - Raise ValueError when validation fails.
    - Generated code should pass static type checking with ty.
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
            },
            assertion_code='''from pathlib import Path

import pytest

from app.specs import parse_user

assert parse_user({"name": "Ada", "age": 31}) == ("Ada", 31)

with pytest.raises(ValueError):
    parse_user({"name": "", "age": 31})

with pytest.raises(ValueError):
    parse_user({"name": "Ada", "age": "31"})

generated_source = (Path.cwd() / "src" / "app" / "__generated__" / "specs.py").read_text(
    encoding="utf-8"
)
assert "pydantic" in generated_source.lower()
''',
            required_packages=("pydantic",),
        ),
        BuiltinEvalCase(
            case_id="example_slugify_smoke",
            description="Example smoke case derived from examples/01_slugify.",
            files={
                "src/slugify_demo/__init__.py": "",
                "src/slugify_demo/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic()
def slugify(title: str) -> str:
    """
    Convert a human title into a URL-safe slug.

    Rules:
    - Trim surrounding whitespace.
    - Lowercase.
    - Replace any run of non-alphanumeric characters with a single "-".
    - Strip leading/trailing "-".
    - Must return a non-empty string.

    Errors:
    - Raise ValueError if title is empty or becomes empty after cleaning.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=slugify)
def post_slug(title: str, *, post_id: int) -> str:
    """
    Create a stable post slug with a numeric suffix.

    - Uses slugify(title) for the base slug.
    - Suffix format: "<base>-<post_id>".
    - post_id must be >= 1.
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
            },
            assertion_code='''import pytest

from slugify_demo.specs import post_slug, slugify

assert slugify("  Hello, World!  ") == "hello-world"
assert slugify("C++ > Java") == "c-java"

with pytest.raises(ValueError):
    slugify("---")

assert post_slug("Hello", post_id=42) == "hello-42"

with pytest.raises(ValueError):
    post_slug("Hello", post_id=0)
''',
        ),
        BuiltinEvalCase(
            case_id="example_dice_smoke",
            description="Example smoke case derived from examples/03_dice_roller.",
            files={
                "src/dice_demo/__init__.py": "",
                "src/dice_demo/specs.py": '''from __future__ import annotations

import random

import jaunt


@jaunt.magic()
def parse_dice(expr: str) -> tuple[int, int, int]:
    """
    Parse dice expressions like "d6", "2d6+3", "2d6-1".

    Return: (count, sides, bonus)

    Rules:
    - Allow surrounding whitespace.
    - "d6" means (1, 6, 0).
    - count and sides must be >= 1.
    - bonus defaults to 0 and may be negative.
    - Raise ValueError on invalid syntax.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=parse_dice)
def roll(expr: str, *, rng: random.Random) -> int:
    """
    Roll a dice expression using a provided RNG and return the total.

    - Uses parse_dice(expr) to parse inputs.
    - Rolls count times with rng.randint(1, sides).
    - Returns sum(rolls) + bonus.

    Determinism example:
    - With rng=random.Random(0), roll("2d6+3", rng=rng) == 11.
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
            },
            assertion_code='''import random

import pytest

from dice_demo.specs import parse_dice, roll

assert parse_dice("d6") == (1, 6, 0)
assert parse_dice("2d6+3") == (2, 6, 3)
assert parse_dice("2d6-1") == (2, 6, -1)

with pytest.raises(ValueError):
    parse_dice("0d6")

rng = random.Random(0)
assert roll("2d6+3", rng=rng) == 11
''',
        ),
    ]
