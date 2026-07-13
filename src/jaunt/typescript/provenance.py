"""Strict parsing and canonical rendering for managed TypeScript artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ManagedDocument:
    """One provenance header and the body following its mandatory separator."""

    fields: Mapping[str, str]
    body: str
    malformed: bool = False


def canonical_managed_body(source: str) -> str:
    """Normalize irrelevant edge whitespace and line endings before hashing."""

    normalized = source.replace("\r\n", "\n").replace("\r", "\n").lstrip()
    return f"{normalized.rstrip()}\n"


def _valid_key(value: str) -> bool:
    return bool(value) and all(
        character == "_" or character.isdigit() or character.islower() for character in value
    )


def parse_managed_document(
    source: str,
    marker: str,
    *,
    allow_bom: bool = False,
) -> ManagedDocument | None:
    """Parse an exact, contiguous ``// jaunt:key=value`` header.

    Unknown fields remain forward-compatible, while duplicate fields, malformed
    header lines, and a missing blank separator are rejected. A Jaunt-looking
    comment in the body can therefore never impersonate provenance metadata.
    """

    body = source.lstrip("\ufeff") if allow_bom else source
    lines = body.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != marker:
        return None

    fields: dict[str, str] = {}
    malformed = False
    cursor = 1
    separated = False
    while cursor < len(lines):
        line = lines[cursor].rstrip("\r\n")
        if not line.strip():
            cursor += 1
            separated = True
            break
        if not line.startswith("// jaunt:"):
            malformed = True
            break
        payload = line.removeprefix("// jaunt:")
        if "=" not in payload:
            malformed = True
            cursor += 1
            continue
        key, value = payload.split("=", 1)
        if not _valid_key(key) or key in fields:
            malformed = True
        else:
            fields[key] = value
        cursor += 1

    if not separated:
        malformed = True
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    return ManagedDocument(fields=fields, body="".join(lines[cursor:]), malformed=malformed)


def render_managed_document(
    marker: str,
    fields: Sequence[tuple[str, str]],
    body: str,
) -> str:
    """Render a managed document whose digestable body is already canonical."""

    canonical = canonical_managed_body(body)
    metadata = "".join(f"// jaunt:{key}={value}\n" for key, value in fields)
    return f"{marker}\n{metadata}\n{canonical}"


__all__ = [
    "ManagedDocument",
    "canonical_managed_body",
    "parse_managed_document",
    "render_managed_document",
]
