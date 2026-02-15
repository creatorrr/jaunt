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
            assertion_code="""from app.specs import add

assert add(2, 3) == 5
assert add(-2, 3) == 1
assert add(0, 0) == 0
""",
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
            assertion_code="""from app.specs import summarize_scores

assert summarize_scores([]) == {"count": 0, "total": 0, "average": 0.0}
assert summarize_scores([10, 20, 30]) == {"count": 3, "total": 60, "average": 20.0}
""",
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
            assertion_code="""import pytest

from app.specs import NameFormatter

fmt = NameFormatter(" Team ")
assert fmt.normalized_prefix == "team"
assert fmt.format_name("  Ada ") == "team:Ada"

with pytest.raises(ValueError):
    NameFormatter("   ")

with pytest.raises(ValueError):
    fmt.format_name("   ")
""",
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
            assertion_code="""from app.specs import make_slug

assert make_slug("  Hello   World  ") == "hello-world"
assert make_slug("Python 3.12!!!") == "python-312"
""",
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
            assertion_code="""from pathlib import Path

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
""",
            required_packages=("pydantic",),
        ),
        BuiltinEvalCase(
            case_id="async_retry_flow",
            description="Async function with retries, error handling, and callable dependency.",
            files={
                "src/app/__init__.py": "",
                "src/app/specs.py": '''from __future__ import annotations

from typing import Awaitable, Callable

import jaunt


@jaunt.magic(
    prompt=(
        "Generated code must pass ty static checking. "
        "Do not leave an implicit None path in this async function; "
        "use an explicit final raise for exhausted retries."
    )
)
async def fetch_with_retries(
    fetcher: Callable[[], Awaitable[str]],
    attempts: int,
) -> str:
    """
    Fetch text with retry behavior.

    Rules:
    - attempts must be >= 1, else raise ValueError.
    - Call `await fetcher()` up to `attempts` times.
    - Retry only when fetcher raises TimeoutError.
    - Return the first successful string result.
    - If all attempts raise TimeoutError, re-raise TimeoutError.
    - Function must satisfy static typing without implicit None return paths.
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
            },
            assertion_code="""import asyncio

import pytest

from app.specs import fetch_with_retries


class _Flaky:
    def __init__(self, fail_count: int) -> None:
        self.remaining = fail_count
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise TimeoutError("temporary")
        return "ok"


async def _run() -> None:
    flaky = _Flaky(fail_count=2)
    out = await fetch_with_retries(flaky, attempts=3)
    assert out == "ok"
    assert flaky.calls == 3

    always_timeout = _Flaky(fail_count=5)
    with pytest.raises(TimeoutError):
        await fetch_with_retries(always_timeout, attempts=2)
    assert always_timeout.calls == 2

    with pytest.raises(ValueError):
        await fetch_with_retries(flaky, attempts=0)


asyncio.run(_run())
""",
        ),
        BuiltinEvalCase(
            case_id="multi_module_chain",
            description="Three-module dependency chain with deterministic aggregation output.",
            files={
                "src/app/__init__.py": "",
                "src/app/parser_specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic()
def parse_kv_line(raw: str) -> tuple[str, int]:
    """
    Parse a single key/value line in the form "name=number".

    Rules:
    - Trim surrounding whitespace.
    - Key must be non-empty and lowercase alphabetic only.
    - Value must parse as a base-10 integer.
    - Raise ValueError for invalid format/input.
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
                "src/app/agg_specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic(deps="app.parser_specs:parse_kv_line")
def combine_lines(lines: list[str]) -> dict[str, int]:
    """
    Parse all lines and sum values by key.

    - Uses parse_kv_line for every line.
    - Return mapping of key -> summed value.
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
                "src/app/report_specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic(deps="app.agg_specs:combine_lines")
def summarize(lines: list[str]) -> str:
    """
    Produce a deterministic report from key/value lines.

    - Uses combine_lines(lines).
    - Sort keys lexicographically.
    - Emit one line per key in the format "<key>=<sum>".
    - Append final line "total=<overall_sum>".
    - Join lines with "\\n".
    """
    raise RuntimeError("spec stub (generated at build time)")
''',
            },
            assertion_code="""import pytest

from app.agg_specs import combine_lines
from app.report_specs import summarize

lines = ["a=1", "b=2", "a=3", "b=4"]
assert combine_lines(lines) == {"a": 4, "b": 6}
assert summarize(lines) == "a=4\\nb=6\\ntotal=10"

with pytest.raises(ValueError):
    summarize(["bad-line"])
""",
        ),
        BuiltinEvalCase(
            case_id="stateful_inventory_class",
            description="Stateful class with mutation semantics and defensive snapshot behavior.",
            files={
                "src/app/__init__.py": "",
                "src/app/specs.py": '''from __future__ import annotations

import jaunt


@jaunt.magic()
class Inventory:
    """
    Track item quantities in memory.

    Behavior:
    - add(name, qty): qty must be > 0, else ValueError.
    - remove(name, qty): qty must be > 0, else ValueError.
      - Raise KeyError if name does not exist.
      - Raise ValueError if qty exceeds current quantity.
      - Remove item entirely when quantity reaches 0.
    - quantity(name): return current quantity or 0 if missing.
    - total_items(): sum of all quantities.
    - snapshot property: return a copy of internal mapping.
    """

    def __init__(self) -> None:
        raise RuntimeError("spec stub (generated at build time)")

    def add(self, name: str, qty: int) -> None:
        raise RuntimeError("spec stub (generated at build time)")

    def remove(self, name: str, qty: int) -> None:
        raise RuntimeError("spec stub (generated at build time)")

    def quantity(self, name: str) -> int:
        raise RuntimeError("spec stub (generated at build time)")

    def total_items(self) -> int:
        raise RuntimeError("spec stub (generated at build time)")

    @property
    def snapshot(self) -> dict[str, int]:
        raise RuntimeError("spec stub (generated at build time)")
''',
            },
            assertion_code="""import pytest

from app.specs import Inventory

inv = Inventory()
inv.add("apple", 3)
inv.add("banana", 2)
inv.add("apple", 2)
assert inv.quantity("apple") == 5
assert inv.quantity("banana") == 2
assert inv.total_items() == 7

snap = inv.snapshot
snap["apple"] = 99
assert inv.quantity("apple") == 5

inv.remove("apple", 4)
assert inv.quantity("apple") == 1
inv.remove("apple", 1)
assert inv.quantity("apple") == 0

with pytest.raises(KeyError):
    inv.remove("apple", 1)

with pytest.raises(ValueError):
    inv.add("pear", 0)

with pytest.raises(ValueError):
    inv.remove("banana", -1)

with pytest.raises(ValueError):
    inv.remove("banana", 5)
""",
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
            assertion_code="""import pytest

from slugify_demo.specs import post_slug, slugify

assert slugify("  Hello, World!  ") == "hello-world"
assert slugify("C++ > Java") == "c-java"

with pytest.raises(ValueError):
    slugify("---")

assert post_slug("Hello", post_id=42) == "hello-42"

with pytest.raises(ValueError):
    post_slug("Hello", post_id=0)
""",
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
            assertion_code="""import random

import pytest

from dice_demo.specs import parse_dice, roll

assert parse_dice("d6") == (1, 6, 0)
assert parse_dice("2d6+3") == (2, 6, 3)
assert parse_dice("2d6-1") == (2, 6, -1)

with pytest.raises(ValueError):
    parse_dice("0d6")

rng = random.Random(0)
assert roll("2d6+3", rng=rng) == 11
""",
        ),
    ]
