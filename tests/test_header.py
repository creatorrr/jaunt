from __future__ import annotations

import json

from jaunt.header import (
    HEADER_MARKER,
    extract_base_api_digest,
    extract_generation_fingerprint,
    extract_module_api_digest,
    extract_module_context_digest,
    extract_module_digest,
    format_header,
    parse_header,
)


def test_format_header_emits_exact_lines_and_parse_roundtrips() -> None:
    hdr = format_header(
        tool_version="0.1.0",
        kind="build",
        source_module="my_project.feature",
        module_digest="deadbeef",
        spec_refs=["my_project.feature:Thing", "my_project.feature:other"],
    )

    lines = hdr.splitlines()
    assert lines[0] == HEADER_MARKER
    assert lines[1] == "# jaunt:tool_version=0.1.0"
    assert lines[2] == "# jaunt:kind=build"
    assert lines[3] == "# jaunt:source_module=my_project.feature"
    assert lines[4] == "# jaunt:module_digest=sha256:deadbeef"
    assert lines[5] == "# jaunt:spec_refs=" + json.dumps(
        ["my_project.feature:Thing", "my_project.feature:other"], ensure_ascii=True
    )
    assert len(lines) == 6

    parsed = parse_header(hdr + "\nprint('ok')\n")
    assert parsed is not None
    assert parsed["tool_version"] == "0.1.0"
    assert parsed["kind"] == "build"
    assert parsed["source_module"] == "my_project.feature"
    assert parsed["module_digest"] == "sha256:deadbeef"
    assert json.loads(parsed["spec_refs"]) == [
        "my_project.feature:Thing",
        "my_project.feature:other",
    ]


def test_parse_header_returns_none_without_marker() -> None:
    assert parse_header("# not a jaunt header\nx=1\n") is None


def test_extract_module_digest() -> None:
    hdr = format_header(
        tool_version="0.1.0",
        kind="test",
        source_module="tests.test_feature",
        module_digest="sha256:abc123",
        spec_refs=[],
    )
    assert extract_module_digest(hdr + "x=1\n") == "sha256:abc123"
    assert extract_module_digest("x=1\n") is None


def test_format_header_includes_generation_fingerprint_when_provided() -> None:
    hdr = format_header(
        tool_version="0.1.0",
        kind="build",
        source_module="pkg.specs",
        module_digest="sha256:abc123",
        generation_fingerprint="deadbeef",
        module_context_digest="facefeed",
        module_api_digest="cafefeed",
        spec_refs=[],
    )

    parsed = parse_header(hdr)
    assert parsed is not None
    assert parsed["generation_fingerprint"] == "sha256:deadbeef"
    assert parsed["module_context_digest"] == "sha256:facefeed"
    assert parsed["module_api_digest"] == "sha256:cafefeed"
    assert extract_generation_fingerprint(hdr + "x=1\n") == "sha256:deadbeef"
    assert extract_module_context_digest(hdr + "x=1\n") == "sha256:facefeed"
    assert extract_module_api_digest(hdr + "x=1\n") == "sha256:cafefeed"


def test_base_api_digest_absent_by_default_is_byte_compatible() -> None:
    # A header without base_api_digest must be byte-identical to the pre-feature
    # header: the field is emitted only when non-empty.
    without = format_header(
        tool_version="0.1.0",
        kind="build",
        source_module="pkg.specs",
        module_digest="sha256:abc123",
        spec_refs=[],
    )
    with_empty = format_header(
        tool_version="0.1.0",
        kind="build",
        source_module="pkg.specs",
        module_digest="sha256:abc123",
        base_api_digest="",
        spec_refs=[],
    )
    assert without == with_empty
    assert "base_api_digest" not in without
    assert extract_base_api_digest(without) is None


def test_base_api_digest_written_and_extracted_when_present() -> None:
    hdr = format_header(
        tool_version="0.1.0",
        kind="build",
        source_module="pkg.child",
        module_digest="sha256:abc123",
        module_context_digest="facefeed",
        base_api_digest="beadfeed",
        spec_refs=[],
    )
    parsed = parse_header(hdr)
    assert parsed is not None
    assert parsed["base_api_digest"] == "sha256:beadfeed"
    assert extract_base_api_digest(hdr + "x=1\n") == "sha256:beadfeed"
    assert extract_base_api_digest("x=1\n") is None
