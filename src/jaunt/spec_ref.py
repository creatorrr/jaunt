"""Stable spec identity helpers.

A SpecRef is a stable string identifier for a spec-producing object. Canonical
format is:

    "pkg.mod:Qualname"
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NewType

SpecRef = NewType("SpecRef", str)

_MODULE_ATTR = "__module__"
_QUALNAME_ATTR = "__qualname__"


def _is_valid_module(module: str) -> bool:
    if not module or module.strip() != module:
        return False
    parts = module.split(".")
    return all(p and p.isidentifier() for p in parts)


def _is_valid_qual_part(part: str) -> bool:
    if not part:
        return False
    if part == "<locals>":
        return True
    return part.isidentifier()


def _is_valid_qualname(qualname: str) -> bool:
    if not qualname or qualname.strip() != qualname:
        return False
    return all(_is_valid_qual_part(p) for p in qualname.split("."))


def normalize_spec_ref(s: str) -> SpecRef:
    """Normalize a spec reference to canonical ``module:qualname`` form.

    Rules:
    - If ``s`` is already in colon form, return it unchanged after sanity checks.
    - If ``s`` is in dot shorthand form (``pkg.mod.Qualname``), convert to
      ``pkg.mod:Qualname`` using the last dot as the separator.
    - Allow dotted qualnames in colon form, e.g. ``pkg.mod:Outer.Inner``.
    - Raise ``ValueError`` for obviously invalid inputs.
    """

    if not isinstance(s, str):
        raise TypeError("spec ref must be a str")

    raw = s.strip()
    if not raw:
        raise ValueError("spec ref must be non-empty")

    if ":" in raw:
        if raw.count(":") != 1:
            raise ValueError("spec ref must contain at most one ':'")
        module, qualname = raw.split(":", 1)
        if not _is_valid_module(module) or not _is_valid_qualname(qualname):
            raise ValueError("invalid spec ref")
        return SpecRef(f"{module}:{qualname}")

    # dot shorthand: split on last dot
    if "." not in raw:
        raise ValueError("dot shorthand spec ref must contain at least one '.'")

    module, qualname = raw.rsplit(".", 1)
    if not _is_valid_module(module) or not _is_valid_qualname(qualname):
        raise ValueError("invalid spec ref")
    return SpecRef(f"{module}:{qualname}")


def spec_ref_from_object(obj: object) -> SpecRef:
    """Derive a canonical SpecRef from an object or from its explicit override."""

    override = getattr(obj, "__jaunt_spec_ref__", None)
    if override is not None:
        if not isinstance(override, str):
            raise TypeError("__jaunt_spec_ref__ must be a str")
        return normalize_spec_ref(override)

    # Use getattr for typing: objects passed here may not be statically known to
    # have these attributes.
    module = getattr(obj, _MODULE_ATTR)
    qualname = getattr(obj, _QUALNAME_ATTR)
    return normalize_spec_ref(f"{module}:{qualname}")


def normalize_spec_refs(value: object) -> tuple[SpecRef, ...]:
    """Normalize an object/string/or-collection into sorted canonical spec refs."""

    if value is None:
        return ()

    items: list[object]
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, Iterable):
        items = list(value)
    else:
        items = [value]

    refs: list[SpecRef] = []
    for item in items:
        if isinstance(item, str):
            refs.append(normalize_spec_ref(item))
        else:
            refs.append(spec_ref_from_object(item))
    refs.sort(key=str)
    return tuple(refs)
