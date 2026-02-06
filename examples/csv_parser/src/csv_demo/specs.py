"""
CSV → Typed Records — Jaunt Example

Parse CSV text into typed dataclass instances with validation.
"""

from __future__ import annotations

from typing import TypeVar

import jaunt

T = TypeVar("T")


@jaunt.magic()
def parse_csv(
    text: str,
    target: type[T],
    *,
    delimiter: str = ",",
    strict: bool = True,
) -> list[T]:
    """
    Parse CSV text into a list of dataclass instances.

    Rules:
    - First row is the header row; column names map to dataclass field names.
      Header cells are whitespace-trimmed before matching.
    - Subsequent rows are data rows.
    - Automatically coerce values to field types (supports str, int, float, bool).
    - For `str` fields, the parsed value is the whitespace-trimmed cell.
    - Bool coercion is case-insensitive after trimming:
      - Truthy: "true", "1", "yes"
      - Falsy: "false", "0", "no"
    - Strip whitespace from all cell values before coercion for all types.
    - Ignore empty trailing rows (rows where all cells are empty or whitespace).
    - Must support dataclasses defined under `from __future__ import annotations`.
      Resolve field types using `typing.get_type_hints(target)` (not `dataclasses.Field.type`)
      so annotations like "str" / "bool" are handled correctly.

    Strict mode (strict=True):
    - Raise ValueError if any header column doesn't match a dataclass field.
    - Raise ValueError if a required field is missing from the headers.
    - Raise TypeError if a value can't be coerced to the target field type.
    - Raise ValueError if any row is missing required non-Optional values
      (either by missing cells or empty cells after trimming).

    Lenient mode (strict=False):
    - Ignore extra columns not in the dataclass.
    - If a header column is missing for a field:
      - Use the field's default/default_factory when present.
      - Use None when the field type is Optional[T] / Union[T, None].
      - Otherwise (required non-Optional with no default), raise ValueError.
    - Skip rows where coercion fails instead of raising (bad rows are dropped).
    - Skip empty rows (all cells empty after trimming) anywhere in the data.

    Errors:
    - Raise TypeError if `target` is not a dataclass.
    - Raise ValueError if text is empty or has no data rows.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=[parse_csv])
def parse_csv_file(
    path: str,
    target: type[T],
    *,
    encoding: str = "utf-8",
    delimiter: str = ",",
    strict: bool = True,
) -> list[T]:
    """
    Read a file at `path` and parse it with parse_csv().

    - Raise FileNotFoundError if path doesn't exist.
    - Raise UnicodeDecodeError if file doesn't match encoding.
    """
    raise RuntimeError("spec stub (generated at build time)")
