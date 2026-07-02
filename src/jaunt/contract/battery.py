"""Render, parse, and merge committed contract test batteries (plain pytest)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from jaunt.header import (
    CONTRACT_BATTERY_MARKER,
    format_contract_battery_header,
    parse_contract_battery_header,
)


@dataclass(frozen=True, slots=True)
class DerivedRegion:
    region_id: str
    code: str


@dataclass(frozen=True, slots=True)
class ParsedBattery:
    header: dict[str, str] | None
    regions: dict[str, str]
    preserved: str


def _begin(region_id: str) -> str:
    return f"# >>> jaunt:derived {region_id}"


def _end(region_id: str) -> str:
    return f"# <<< jaunt:derived {region_id}"


def _header_text(header_fields: dict[str, str]) -> str:
    return format_contract_battery_header(
        derived_from=header_fields["derived_from"],
        prose_digest=header_fields["prose_digest"],
        signature=header_fields["signature"],
        body_digest=header_fields["body_digest"],
        strength=header_fields["strength"],
        tool_version=header_fields["tool_version"],
    )


def _region_block(region: DerivedRegion) -> str:
    return f"{_begin(region.region_id)}\n{region.code.rstrip()}\n{_end(region.region_id)}"


def render_battery(
    *,
    import_module: str,
    func_name: str,
    regions: list[DerivedRegion],
    header_fields: dict[str, str],
    preserved: str = "",
    extra_imports: tuple[str, ...] = (),
) -> str:
    parts = [
        _header_text(header_fields).rstrip(),
        "import pytest",
        f"from {import_module} import {func_name}",
    ]
    parts += [f"from {import_module} import {name}" for name in extra_imports]
    parts.append("")
    for region in regions:
        parts.append(_region_block(region))
        parts.append("")
    body = "\n".join(parts).rstrip() + "\n"
    if preserved.strip():
        body += "\n\n" + preserved.strip() + "\n"
    return body


_REGION_RE = re.compile(
    r"^# >>> jaunt:derived (?P<rid>\S+)\n(?P<code>.*?)\n# <<< jaunt:derived (?P=rid)\s*$",
    re.DOTALL | re.MULTILINE,
)


def parse_battery(source: str) -> ParsedBattery:
    header = parse_contract_battery_header(source)

    regions: dict[str, str] = {}
    for m in _REGION_RE.finditer(source):
        regions[m.group("rid")] = m.group("code")

    # Preserved = everything with header lines, the generated import preamble,
    # and derived regions removed. What remains is hand-added content.
    #
    # The preamble (header + jaunt import lines) is only stripped while we are
    # still inside the contiguous generated block at the top of the file. Once a
    # line of real content appears, import-like lines are left untouched so that
    # hand-added imports below the derived markers survive a merge.
    stripped = _REGION_RE.sub("", source)
    out_lines: list[str] = []
    in_preamble = stripped.splitlines()[:1] == [CONTRACT_BATTERY_MARKER]
    for line in stripped.splitlines():
        if in_preamble:
            if line == CONTRACT_BATTERY_MARKER or line.startswith("# jaunt:"):
                continue
            if line.strip() == "import pytest":
                continue
            if re.match(r"^from \S+ import \S+$", line.strip()):
                continue
            if not line.strip():
                continue
            # First real line of content ends the generated preamble.
            in_preamble = False
        out_lines.append(line)
    preserved = "\n".join(out_lines).strip()
    return ParsedBattery(header=header, regions=regions, preserved=preserved)


def de_jaunt_battery(source: str, *, provenance: str) -> str:
    """Turn a jaunt battery into a plain, hand-owned pytest module."""

    # Drop the contract header lines.
    lines = source.splitlines()
    body_lines: list[str] = []
    in_header = lines[:1] == [CONTRACT_BATTERY_MARKER] or lines[:1] == [
        "# This file is maintained by jaunt (contract mode). Review like any test."
    ]
    legacy_marker = "# This file is maintained by jaunt (contract mode). Review like any test."
    for line in lines:
        if in_header and (
            line == CONTRACT_BATTERY_MARKER or line == legacy_marker or line.startswith("# jaunt:")
        ):
            continue
        in_header = False
        # Drop derived-region markers but keep the code between them.
        if line.startswith("# >>> jaunt:derived ") or line.startswith("# <<< jaunt:derived "):
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return f"# {provenance} (ejected from jaunt contract mode; now hand-owned).\n{body}\n"


def merge_battery(
    existing: str | None,
    *,
    import_module: str,
    func_name: str,
    regions: list[DerivedRegion],
    header_fields: dict[str, str],
    extra_imports: tuple[str, ...] = (),
) -> str:
    preserved = ""
    if existing is not None:
        preserved = parse_battery(existing).preserved
    return render_battery(
        import_module=import_module,
        func_name=func_name,
        regions=regions,
        header_fields=header_fields,
        preserved=preserved,
        extra_imports=extra_imports,
    )
