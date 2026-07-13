"""TypeScript contract adopt, reconcile, and eject lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import math
import os
import posixpath
import re
import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from jaunt.cache import ResponseCache
from jaunt.config import JauntConfig
from jaunt.cost import CostTracker
from jaunt.errors import JauntConfigError, JauntGenerationError
from jaunt.generate.base import GenerationRequest, GeneratorBackend, TokenUsage
from jaunt.generate.request_cache import generate_request_cached
from jaunt.journal import JournalEvent, append_events
from jaunt.skill_seed import skills_fingerprint
from jaunt.targets.base import TargetDiagnostic, TargetStatus
from jaunt.typescript.builder import (
    MISSING_INPUT,
    WorkerFactory,
    _default_backend,
    _progress_advance,
    _progress_finish,
    _progress_phase,
    _safe_path,
    _sha256,
    _target,
    _Write,
    analyze,
    atomic_write_manifest,
    worker_session,
)
from jaunt.typescript.properties import (
    PROPERTY_RENDERER_SCHEME,
    attach_property_block,
    parse_property_cases,
    render_property_block,
)
from jaunt.typescript.provenance import (
    canonical_managed_body,
    parse_managed_document,
    render_managed_document,
)
from jaunt.typescript.tester import (
    _async_export_names,
    _fixture_for_path,
    _fixture_names,
    _generated_test_files,
    _implicit_class_test_specs,
    _owner_project_for_source,
    _runner_fingerprint,
    _run_test_batches,
    _strip_test_header,
    _static_test_validation,
    _test_output,
    _terminate_runner_process,
    _validate_test_owner_dependencies,
    _workspace_project_config_paths,
    _workspace_test_file_owners,
)
from jaunt.typescript.worker import worker_environment

_CONTRACT_TAG = "@jauntContract"
_BATTERY_HEADER = "// ⚙️ jaunt:contract-battery — DO NOT EDIT. Regenerate with `jaunt reconcile`."
_MUTATION_SCHEME = "jaunt-ts-mutation/1"
# Each isolated mutant performs a strict TypeScript check and then starts Vitest.
# Keep that startup budget bounded but large enough for ordinary workspaces; the
# coordinator's global deadline still caps the complete strength run.
_MUTATION_TIMEOUT_SECONDS = 15.0
_MUTATION_GLOBAL_TIMEOUT_SECONDS = 120.0
_MUTATION_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LifecycleReport:
    command: str
    targets: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    proposed: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[TargetDiagnostic, ...] = ()
    usage: Mapping[str, object] | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _read_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _line_ending(source: str) -> str:
    return "\r\n" if "\r\n" in source else "\n"


def _split_target(root: Path, target: str) -> tuple[Path, str]:
    if "#" not in target:
        raise JauntConfigError("A TypeScript contract target must end in #<export-name>")
    path_part, symbol = target.rsplit("#", 1)
    if path_part.startswith("ts:"):
        stable_path = path_part[3:]
        candidates = (
            [stable_path]
            if Path(stable_path).suffix in {".ts", ".tsx"}
            else [f"{stable_path}.ts", f"{stable_path}.tsx"]
        )
        existing = [candidate for candidate in candidates if _safe_path(root, candidate).is_file()]
        if len(existing) > 1:
            raise JauntConfigError(f"TypeScript contract target is ambiguous: {path_part}")
        path_part = existing[0] if existing else candidates[0]
    path = _safe_path(root, path_part)
    if not path.is_file():
        raise JauntConfigError(f"TypeScript contract source does not exist: {path_part}")
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", symbol):
        raise JauntConfigError(f"Invalid TypeScript export name: {symbol!r}")
    return path, symbol


def _declaration_start(source: str, symbol: str) -> int:
    matches = list(
        re.finditer(
            rf"(?m)^\s*export\s+(?:(?:default|declare|async|abstract)\s+)*(?P<kind>function|class)\s+{re.escape(symbol)}\b",
            source,
        )
    )
    if not matches or (matches[0].group("kind") == "class" and len(matches) != 1):
        raise JauntConfigError(
            f"Expected one exported class or an overload group named {symbol!r}; "
            f"found {len(matches)} declarations"
        )
    return matches[0].start()


def _preceding_docs(source: str, declaration_start: int) -> tuple[int, int] | None:
    prefix = source[:declaration_start]
    matches = list(re.finditer(r"/\*\*.*?\*/", prefix, re.DOTALL))
    if not matches:
        return None
    match = matches[-1]
    if prefix[match.end() :].strip():
        return None
    return match.start(), match.end()


def _projection_offset(projection: Mapping[str, object], key: str, source: str) -> int:
    """Translate a TypeScript UTF-16 offset to a Python string index.

    TypeScript reports ``Node.getStart()``/``Node.end`` in JavaScript UTF-16
    code units, while Python slices Unicode scalar values.  Astral characters
    therefore occupy two worker positions but one Python position.  Reject an
    offset inside that surrogate pair instead of silently moving an edit into
    authored source.
    """

    value = projection.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JauntConfigError(f"TypeScript worker returned an invalid {key} offset")
    utf16_cursor = 0
    for index, character in enumerate(source):
        if utf16_cursor == value:
            return index
        utf16_cursor += 2 if ord(character) > 0xFFFF else 1
        if utf16_cursor > value:
            break
    if utf16_cursor == value:
        return len(source)
    raise JauntConfigError(f"TypeScript worker returned an invalid {key} offset")


def _contract_source_ranges(
    source: str,
    symbol: str,
    projection: Mapping[str, object],
) -> tuple[int, int, tuple[int, int] | None]:
    """Validate AST-owned source ranges before any contract marker edit."""

    _declaration_only_contract(source, symbol, projection)
    declaration_start = _projection_offset(projection, "declarationStart", source)
    declaration_end = _projection_offset(projection, "declarationEnd", source)
    if declaration_start >= declaration_end:
        raise JauntConfigError("TypeScript worker returned an invalid declaration range")
    docs_start = projection.get("docsStart")
    docs_end = projection.get("docsEnd")
    if docs_start is None and docs_end is None:
        return declaration_start, declaration_end, None
    if docs_start is None or docs_end is None:
        raise JauntConfigError("TypeScript worker returned a partial TSDoc range")
    start = _projection_offset(projection, "docsStart", source)
    end = _projection_offset(projection, "docsEnd", source)
    if start >= end or end > declaration_start or not source[start:end].startswith("/**"):
        raise JauntConfigError("TypeScript worker returned an invalid TSDoc range")
    return declaration_start, declaration_end, (start, end)


def _contract_tag_offsets(block: str) -> tuple[tuple[int, int], ...]:
    offsets: list[tuple[int, int]] = []
    cursor = 0
    identifier = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$"
    while (start := block.find(_CONTRACT_TAG, cursor)) >= 0:
        end = start + len(_CONTRACT_TAG)
        before = block[start - 1] if start else ""
        after = block[end] if end < len(block) else ""
        if (not before or before not in identifier) and (not after or after not in identifier):
            offsets.append((start, end))
        cursor = end
    return tuple(offsets)


def _add_contract_tag(
    source: str,
    symbol: str,
    projection: Mapping[str, object],
) -> str:
    declaration_start, _declaration_end, docs = _contract_source_ranges(source, symbol, projection)
    if docs is None:
        line_start = source.rfind("\n", 0, declaration_start) + 1
        indent = source[line_start:declaration_start]
        if indent.strip():
            raise JauntConfigError("TypeScript declaration does not begin on a clean source line")
        marker = f"/** {_CONTRACT_TAG} */{_line_ending(source)}{indent}"
        return source[:declaration_start] + marker + source[declaration_start:]

    docs_start, docs_end = docs
    block = source[docs_start:docs_end]
    if _contract_tag_offsets(block):
        return source
    closing = block.rfind("*/")
    if closing < 0:
        raise JauntConfigError("TypeScript worker returned an unterminated TSDoc range")
    closing_line = block.rfind("\n", 0, closing) + 1
    closing_prefix = block[closing_line:closing]
    if closing_line > 0 and not closing_prefix.strip():
        insertion = f"{closing_prefix}* {_CONTRACT_TAG}{_line_ending(source)}"
        replacement = block[:closing_line] + insertion + block[closing_line:]
    else:
        # Tabs make the inline edit byte-for-byte reversible without guessing
        # whether surrounding spaces belonged to the authored one-line TSDoc.
        replacement = block[:closing] + f"\t{_CONTRACT_TAG}\t" + block[closing:]
    return source[:docs_start] + replacement + source[docs_end:]


def _remove_contract_tag(
    source: str,
    symbol: str,
    projection: Mapping[str, object],
) -> str:
    _declaration_start_offset, _declaration_end, docs = _contract_source_ranges(
        source, symbol, projection
    )
    if docs is None:
        raise JauntConfigError(f"{symbol!r} is not marked with {_CONTRACT_TAG}")
    docs_start, docs_end = docs
    block = source[docs_start:docs_end]
    offsets = _contract_tag_offsets(block)
    if len(offsets) != 1:
        raise JauntConfigError(
            f"Expected one {_CONTRACT_TAG} marker for {symbol!r}; found {len(offsets)}"
        )
    start, end = offsets[0]

    if block.strip() == f"/** {_CONTRACT_TAG} */":
        line_start = source.rfind("\n", 0, docs_start) + 1
        line_end = docs_end
        if source.startswith("\r\n", line_end):
            line_end += 2
        elif line_end < len(source) and source[line_end] in "\r\n":
            line_end += 1
        return source[:line_start] + source[line_end:]

    cursor = 0
    for line in block.splitlines(keepends=True):
        if line.strip() in {f"* {_CONTRACT_TAG}", _CONTRACT_TAG}:
            replacement = block[:cursor] + block[cursor + len(line) :]
            return source[:docs_start] + replacement + source[docs_end:]
        cursor += len(line)

    if start > 0 and end < len(block) and block[start - 1] == block[end] == "\t":
        start -= 1
        end += 1
    replacement = block[:start] + block[end:]
    return source[:docs_start] + replacement + source[docs_end:]


def _battery_path(root: Path, config: JauntConfig, source: Path, symbol: str) -> Path:
    target = config.typescript_target
    if target is None:
        raise JauntConfigError("No [target.ts] is configured")
    relative = source.relative_to(root)
    safe_symbol = re.sub(r"[^A-Za-z0-9_.-]+", "-", symbol)
    return _safe_path(
        root,
        (
            Path(target.contract_battery_dir)
            / relative.parent
            / f"{relative.stem}.{safe_symbol}.contract.test.ts"
        ).as_posix(),
    )


def _precondition(path: Path) -> str:
    return _sha256(path.read_bytes()) if path.exists() else MISSING_INPUT


def _facade_specifier(battery: Path, source: Path) -> str:
    relative = posixpath.relpath(source.with_suffix(".js").as_posix(), battery.parent.as_posix())
    return relative if relative.startswith(".") else f"./{relative}"


def _declaration_only_contract(
    source: str,
    symbol: str,
    projection: Mapping[str, object],
) -> str:
    """Validate the TypeScript AST worker's fail-closed declaration projection."""

    projected = projection.get("source")
    projected_symbol = projection.get("symbol")
    kind = projection.get("kind")
    source_digest = projection.get("sourceDigest")
    if (
        not isinstance(projected, str)
        or not projected.strip()
        or projected_symbol != symbol
        or kind not in {"function", "class"}
        or source_digest != _sha256(source.encode("utf-8"))
    ):
        raise JauntConfigError(
            f"TypeScript worker returned an invalid declaration projection for {symbol!r}"
        )
    maximum_projection_length = len(source) + 2 * (source.count("export") + 1)
    if len(projected) > maximum_projection_length:
        raise JauntConfigError(
            f"TypeScript worker declaration projection grew unexpectedly for {symbol!r}"
        )
    return projected.rstrip() + "\n"


async def _project_contract(
    client: object,
    root: Path,
    source: Path,
    symbol: str,
    source_text: str,
) -> Mapping[str, object]:
    request_method = getattr(client, "request", None)
    if request_method is None:
        raise JauntConfigError("TypeScript contract operations require the AST worker")
    projected = await request_method(
        "projectContract",
        {
            "source": source_text,
            "symbol": symbol,
            "fileName": source.relative_to(root).as_posix(),
        },
    )
    if not isinstance(projected, Mapping):
        raise JauntConfigError(
            f"TypeScript worker returned an invalid declaration projection for {symbol!r}"
        )
    _declaration_only_contract(source_text, symbol, projected)
    return projected


def _battery_request(
    root: Path,
    config: JauntConfig,
    source: Path,
    symbol: str,
    battery: Path,
    source_text: str,
    *,
    declaration_context: str,
    builtin_skill_names: Sequence[str] | None = None,
) -> GenerationRequest:
    relative_source = source.relative_to(root).as_posix()
    relative_battery = battery.relative_to(root).as_posix()
    target = config.typescript_target
    assert target is not None
    declaration_start = _declaration_start(source_text, symbol)
    declaration_docs = _preceding_docs(source_text, declaration_start)
    property_source = (
        source_text[declaration_docs[0] : declaration_docs[1]]
        if declaration_docs is not None
        else ""
    )
    case_digest = hashlib.sha256(
        f"{relative_source}#{symbol}\0{_sha256(source_text.encode('utf-8'))}".encode()
    ).digest()
    legacy_property_seed = int.from_bytes(case_digest[:4], "big") & 0x7FFF_FFFF
    exported_symbols = tuple(
        sorted(
            set(
                re.findall(
                    r"\bexport\s+(?:default\s+)?(?:async\s+)?function\s+"
                    r"([A-Za-z_$][\w$]*)\b",
                    source_text,
                )
            )
        )
    )
    fixture_names = _fixture_names(property_source)
    property_cases = parse_property_cases(
        (property_source,),
        label=f"TypeScript contract {relative_source}#{symbol}",
        public_symbols=exported_symbols,
        fixture_names=fixture_names,
        async_symbols=_async_export_names(source_text),
    )
    property_count = len(property_cases)
    property_seed = property_cases[0].seed if property_cases else legacy_property_seed
    fixture = _fixture_for_path(root, relative_battery)
    if fixture_names and fixture is None:
        raise JauntConfigError(
            f"{relative_source} declares fixtures {', '.join(fixture_names)} but no canonical "
            "fixtures.ts or fixtures.tsx exists at the contract battery owner"
        )
    fixture_instruction = ""
    fixture_specifier = ""
    context_files = {"_context/contract-source.ts": declaration_context}
    if property_cases:
        context_files["_context/properties.json"] = (
            json.dumps(
                [case.payload() for case in property_cases],
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
    if fixture is not None:
        fixture_path, fixture_source = fixture
        fixture_specifier = _facade_specifier(battery, _safe_path(root, fixture_path))
        context_files["_context/fixtures.ts"] = fixture_source
        fixture_instruction = (
            f" Import the extended `test` from `{fixture_specifier}` and destructure "
            f"the declared fixtures: {', '.join(fixture_names)}."
            if fixture_names
            else f" The optional typed fixture surface is available from `{fixture_specifier}`."
        )
    property_instruction = (
        "Jaunt parsed every supported `@prop` bullet into `_context/properties.json` and "
        "will append those cases after generation. Do not import fast-check, call "
        "`fc.assert`, or write property tests yourself."
        if property_cases
        else (
            "When the declaration contains `@prop`, render each property with `fc.assert` "
            f"options `{{ seed: {property_seed}, numRuns: {target.fast_check_runs} }}`. "
            "The seed and run count are contract metadata and must be literal, not computed "
            "at runtime. Declare every strategy as "
            "`const nameArbitrary: fc.Arbitrary<ExpectedType> = expression`, never use "
            "`any`, and pass the typed binding to `fc.property` or `fc.asyncProperty`."
        )
    )
    prompt = f"""You are the independent contract-test writer for Jaunt TypeScript.

Write a complete Vitest battery for exported `{symbol}` from
`{_facade_specifier(battery, source)}` to `{relative_battery}`. Derive assertions
only from the implementation's TSDoc contract and type declaration. Cover explicit
examples and named errors, plus deterministic fast-check properties when the docs
contain `@prop`. Do not import private Jaunt specs or generated-private paths. Do
not inspect or copy the implementation body. Use finite deterministic tests and no
snapshots, custom reporters, setup hooks, console output, or TypeScript suppressions.
{property_instruction}{fixture_instruction}
"""
    property_block = render_property_block(
        property_cases,
        symbol_specifiers={
            exported: _facade_specifier(battery, source) for exported in exported_symbols
        },
        num_runs=target.fast_check_runs,
        fixture_specifier=fixture_specifier if fixture_names else "",
        fixture_names=fixture_names,
    )

    def validate(source_code: str) -> list[str]:
        errors = _static_test_validation(source_code)
        if fixture_names:
            if fixture_specifier not in source_code:
                errors.append(
                    f"contract fixture tests must import the extended test from {fixture_specifier}"
                )
            for name in fixture_names:
                if re.search(rf"\{{[^}}]*\b{re.escape(name)}\b[^}}]*\}}", source_code) is None:
                    errors.append(f"contract tests must destructure declared fixture {name}")
        if property_count < 1:
            return errors
        if "__jauntProperty" in source_code:
            errors.append("contract tests must not define Jaunt's reserved property bindings")
        if re.search(r'(?:from\s+|import\s*\()["\']fast-check["\']', source_code):
            errors.append("contract tests must leave deterministic @prop rendering to Jaunt")
        if re.search(r"\bfc\.(?:assert|property|asyncProperty)\b", source_code):
            errors.append("contract tests must leave deterministic @prop rendering to Jaunt")
        return errors

    return GenerationRequest(
        language="ts",
        kind="contract-test",
        target_path=relative_battery,
        context_files=context_files,
        prompt=prompt,
        cache_payload={
            "source": relative_source,
            "symbol": symbol,
            "sourceDigest": _sha256(source_text.encode("utf-8")),
            "propertySeed": property_seed,
            "fastCheckRuns": target.fast_check_runs,
            "propertyCount": property_count,
            "propertyCases": [case.payload() for case in property_cases],
            "propertyBlock": property_block,
        },
        validator=validate,
        project_root=root,
        builtin_skill_names=(
            tuple(builtin_skill_names)
            if builtin_skill_names is not None
            else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
        ),
    )


def _with_header(
    source: str,
    source_path: str,
    source_digest: str,
    property_digest: str = _sha256(b""),
) -> str:
    body = _canonical_battery_body(source)
    body_digest = _sha256(body.encode("utf-8"))
    return render_managed_document(
        _BATTERY_HEADER,
        (
            ("source", source_path),
            ("source_digest", source_digest),
            ("property_scheme", PROPERTY_RENDERER_SCHEME),
            ("property_digest", property_digest),
            ("body_digest", body_digest),
        ),
        body,
    )


def _strip_managed_battery_header(source: str) -> str:
    """Remove the complete variable-length Jaunt header from a battery."""

    parsed = parse_managed_document(source, _BATTERY_HEADER)
    return source if parsed is None else parsed.body


def _canonical_battery_body(source: str) -> str:
    """Return the exact body bytes covered by contract-battery provenance."""

    return canonical_managed_body(_strip_managed_battery_header(source))


def _battery_header_metadata(source: str) -> Mapping[str, str] | None:
    parsed = parse_managed_document(source, _BATTERY_HEADER)
    if parsed is None or parsed.malformed:
        return None
    return parsed.fields


def _battery_body_digest_issue(source: str) -> str | None:
    """Classify invalid body provenance without trusting executable test content."""

    parsed = parse_managed_document(source, _BATTERY_HEADER)
    if parsed is None:
        return "missing-body-digest"
    if parsed.malformed:
        return "malformed-body-digest"
    candidate = parsed.fields.get("body_digest")
    if candidate is None:
        return "missing-body-digest"
    if re.fullmatch(r"sha256:[0-9a-f]{64}", candidate) is None:
        return "malformed-body-digest"
    body_digest = _sha256(canonical_managed_body(parsed.body).encode("utf-8"))
    if candidate != body_digest:
        return "body-digest-mismatch"
    return None


def _strength_cases(report: Mapping[str, Any]) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for outcome in ("killed", "survived", "excluded"):
        raw = report.get(outcome, [])
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            case: dict[str, object] = {
                "id": str(item.get("id", "unknown")),
                "kind": str(item.get("kind", "unsupported")),
                "line": int(item.get("line", 0)),
                "column": int(item.get("column", 0)),
                "outcome": outcome,
            }
            if isinstance(item.get("reason"), str):
                case["reason"] = str(item["reason"])
            cases.append(case)
    return sorted(cases, key=lambda item: str(item["id"]))


def _with_strength_metadata(source: str, report: Mapping[str, Any]) -> str:
    """Record deterministic, non-secret mutation outcomes in the battery header."""

    body = _canonical_battery_body(source)
    body_digest = _sha256(body.encode("utf-8"))
    metadata = _battery_header_metadata(source)
    if metadata is None or "source" not in metadata or "source_digest" not in metadata:
        raise JauntGenerationError("The generated TypeScript contract battery has no provenance")
    _validate_mutation_report(report)
    score = report.get("score")
    assert isinstance(score, Mapping)
    killed = int(score.get("killed", 0))
    applicable = int(score.get("applicable", 0))
    excluded = int(score.get("excluded", 0))
    cases = json.dumps(
        _strength_cases(report),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return render_managed_document(
        _BATTERY_HEADER,
        (
            ("source", metadata["source"]),
            ("source_digest", metadata["source_digest"]),
            ("property_scheme", metadata.get("property_scheme", "")),
            ("property_digest", metadata.get("property_digest", "")),
            ("body_digest", body_digest),
            ("strength_scheme", _MUTATION_SCHEME),
            ("strength", f"{killed}/{applicable}"),
            ("strength_excluded", str(excluded)),
            ("strength_concurrency", "1"),
            ("strength_cases", cases),
        ),
        body,
    )


def _parse_strength_metadata(source: str) -> Mapping[str, object] | None:
    metadata = _battery_header_metadata(source)
    if metadata is None:
        return None
    scheme = metadata.get("strength_scheme")
    score = metadata.get("strength")
    excluded = metadata.get("strength_excluded")
    concurrency = metadata.get("strength_concurrency")
    cases = metadata.get("strength_cases")
    if None in {scheme, score, excluded, concurrency, cases}:
        return None
    assert score is not None
    assert excluded is not None
    assert concurrency is not None
    assert cases is not None
    score_match = re.fullmatch(r"(\d+)/(\d+)", score)
    if score_match is None or not excluded.isdigit() or not concurrency.isdigit():
        return None
    try:
        decoded = json.loads(cases)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list) or any(not isinstance(item, Mapping) for item in decoded):
        return None
    ids = [str(item.get("id", "")) for item in decoded]
    outcomes = [str(item.get("outcome", "")) for item in decoded]
    killed = int(score_match.group(1))
    applicable = int(score_match.group(2))
    excluded_count = int(excluded)
    if (
        not all(ids)
        or ids != sorted(ids)
        or len(ids) != len(set(ids))
        or any(outcome not in {"killed", "survived", "excluded"} for outcome in outcomes)
        or outcomes.count("killed") != killed
        or outcomes.count("killed") + outcomes.count("survived") != applicable
        or outcomes.count("excluded") != excluded_count
        or int(concurrency) != 1
    ):
        return None
    return {
        "scheme": scheme,
        "killed": killed,
        "applicable": applicable,
        "survived": outcomes.count("survived"),
        "excluded": excluded_count,
        "concurrency": int(concurrency),
        "cases": decoded,
    }


def _mutation_runner_path(client: object) -> Path:
    installation = getattr(client, "installation", None)
    package_root = getattr(installation, "package_root", None)
    if not isinstance(package_root, Path):
        raise JauntConfigError("The TypeScript worker installation has no mutation runner")
    path = package_root / "dist" / "test" / "mutation.js"
    if not path.is_file():
        worker_entry = getattr(installation, "worker_entry", None)
        if isinstance(worker_entry, Path) and worker_entry.parent.name == "worker":
            path = worker_entry.parent.parent / "test" / "mutation.js"
    if not path.is_file():
        raise JauntConfigError(f"Installed @usejaunt/ts has no mutation runner at {path}")
    return path


def _mutation_count(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JauntGenerationError(f"TypeScript mutation runner returned invalid {field_name}")
    return value


def _validate_mutation_report(
    report: Mapping[str, Any],
    *,
    source_path: str | None = None,
    symbol: str | None = None,
) -> None:
    """Validate the complete mutation subprocess DTO and all score invariants."""

    required = {
        "protocol",
        "sourcePath",
        "symbol",
        "concurrency",
        "complete",
        "killed",
        "survived",
        "excluded",
        "score",
    }
    if set(report) != required or report.get("protocol") != _MUTATION_SCHEME:
        raise JauntGenerationError("TypeScript mutation runner returned an incompatible report")
    if source_path is not None and report.get("sourcePath") != source_path:
        raise JauntGenerationError("TypeScript mutation runner returned the wrong source path")
    if symbol is not None and report.get("symbol") != symbol:
        raise JauntGenerationError("TypeScript mutation runner returned the wrong symbol")
    if not isinstance(report.get("sourcePath"), str) or not report["sourcePath"]:
        raise JauntGenerationError("TypeScript mutation runner omitted its source path")
    if not isinstance(report.get("symbol"), str) or not report["symbol"]:
        raise JauntGenerationError("TypeScript mutation runner omitted its symbol")
    if report.get("concurrency") != 1 or not isinstance(report.get("complete"), bool):
        raise JauntGenerationError("TypeScript mutation runner returned invalid run metadata")

    records: dict[str, list[Mapping[str, Any]]] = {}
    identifiers: set[str] = set()
    allowed_kinds = {"return", "boolean", "comparison", "throw", "constant", "unsupported"}
    allowed_reasons = {
        "test-failed",
        "timeout",
        "did-not-compile",
        "runner-error",
        "no-mutable-site",
    }
    for outcome in ("killed", "survived", "excluded"):
        raw_records = report.get(outcome)
        if not isinstance(raw_records, list):
            raise JauntGenerationError(
                f"TypeScript mutation runner returned invalid {outcome} records"
            )
        parsed: list[Mapping[str, Any]] = []
        for record in raw_records:
            if not isinstance(record, Mapping):
                raise JauntGenerationError("TypeScript mutation runner returned a malformed case")
            allowed_fields = {"id", "kind", "line", "column", "description", "outcome", "reason"}
            if set(record) - allowed_fields:
                raise JauntGenerationError("TypeScript mutation runner returned a malformed case")
            identifier = record.get("id")
            if (
                not isinstance(identifier, str)
                or not identifier
                or len(identifier) > 128
                or identifier in identifiers
                or record.get("kind") not in allowed_kinds
                or record.get("outcome") != outcome
                or not isinstance(record.get("description"), str)
                or not record["description"]
            ):
                raise JauntGenerationError("TypeScript mutation runner returned a malformed case")
            identifiers.add(identifier)
            _mutation_count(record.get("line"), "case line")
            _mutation_count(record.get("column"), "case column")
            reason = record.get("reason")
            if reason is not None and reason not in allowed_reasons:
                raise JauntGenerationError("TypeScript mutation runner returned a malformed reason")
            parsed.append(record)
        records[outcome] = parsed

    score = report.get("score")
    if not isinstance(score, Mapping) or set(score) != {
        "killed",
        "applicable",
        "survived",
        "excluded",
        "ratio",
    }:
        raise JauntGenerationError("TypeScript mutation runner returned an invalid score")
    killed = _mutation_count(score.get("killed"), "killed score")
    applicable = _mutation_count(score.get("applicable"), "applicable score")
    survived = _mutation_count(score.get("survived"), "survived score")
    excluded = _mutation_count(score.get("excluded"), "excluded score")
    if (
        killed != len(records["killed"])
        or survived != len(records["survived"])
        or excluded != len(records["excluded"])
        or applicable != killed + survived
    ):
        raise JauntGenerationError("TypeScript mutation runner score does not match its cases")
    ratio = score.get("ratio")
    if applicable == 0:
        if ratio is not None:
            raise JauntGenerationError("TypeScript mutation runner returned an invalid ratio")
    elif (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not math.isfinite(float(ratio))
        or not math.isclose(float(ratio), killed / applicable, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise JauntGenerationError("TypeScript mutation runner returned an invalid ratio")


async def _terminate_mutation_process(process: Any, *, platform: str | None = None) -> None:
    """Terminate the isolated mutation runner and every process in its tree."""

    if process.returncode is not None:
        return
    effective = os.name if platform is None else platform
    if effective != "posix":
        # The shared runner helper uses taskkill /T /F on Windows and retains a
        # defensive leader-only fallback for other runtimes.
        await _terminate_runner_process(process, platform=effective)
        return

    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def _run_mutation_strength(
    client: object,
    root: Path,
    config: JauntConfig,
    *,
    source_path: str,
    symbol: str,
    battery_file: str,
    owner_project: str,
    overlays: Mapping[str, str],
    timeout: float = _MUTATION_TIMEOUT_SECONDS,
    global_timeout: float = _MUTATION_GLOBAL_TIMEOUT_SECONDS,
) -> Mapping[str, Any]:
    """Run the package coordinator in an isolated process tree."""

    target = config.typescript_target
    if target is None:
        raise JauntConfigError("No [target.ts] is configured")
    installation = getattr(client, "installation", None)
    node = getattr(installation, "node", None)
    compiler = getattr(installation, "compiler_module_path", None)
    if not isinstance(node, str) or not isinstance(compiler, Path):
        raise JauntConfigError("The TypeScript worker installation is incomplete")
    mutation_runner = _mutation_runner_path(client)
    payload: dict[str, object] = {
        "root": str(root),
        "sourcePath": source_path,
        "symbol": symbol,
        "batteryFiles": [battery_file],
        "overlays": dict(overlays),
        "tsconfigPath": owner_project,
        "compilerModulePath": str(compiler),
        "timeoutMs": max(1, int(timeout * 1000)),
        "globalTimeoutMs": max(1, int(global_timeout * 1000)),
    }
    if target.vitest_config:
        payload["vitestConfigPath"] = target.vitest_config
    process = await asyncio.create_subprocess_exec(
        node,
        str(mutation_runner),
        cwd=str(root),
        env=worker_environment(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(json.dumps(payload, sort_keys=True).encode("utf-8")),
            timeout=global_timeout + 10.0,
        )
    except TimeoutError:
        await _terminate_mutation_process(process)
        raise JauntGenerationError("TypeScript contract mutation strength timed out") from None
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_mutation_process(process))
        raise
    if process.returncode != 0 or len(stdout) > _MUTATION_MAX_OUTPUT_BYTES or not stdout.strip():
        raise JauntGenerationError("TypeScript contract mutation runner failed")
    try:
        result = json.loads(stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise JauntGenerationError("TypeScript mutation runner returned invalid JSON") from error
    if not isinstance(result, Mapping):
        raise JauntGenerationError("TypeScript mutation runner returned an incompatible report")
    _validate_mutation_report(result, source_path=source_path, symbol=symbol)
    return result


def _mutation_strength_diagnostics(
    report: Mapping[str, Any], source_path: str
) -> tuple[TargetDiagnostic, ...]:
    _validate_mutation_report(report)
    if report.get("complete") is not True:
        return (
            TargetDiagnostic(
                code="JAUNT_TS_CONTRACT_STRENGTH_INCOMPLETE",
                message=(
                    "Mutation strength did not finish. No contract batteries were changed; "
                    "rerun reconcile after checking runner timeouts."
                ),
                path=source_path,
            ),
        )
    diagnostics: list[TargetDiagnostic] = []
    raw_survivors = report.get("survived", [])
    survivors = raw_survivors if isinstance(raw_survivors, list) else []
    for survivor in survivors:
        if not isinstance(survivor, Mapping):
            continue
        line_value = survivor.get("line")
        column_value = survivor.get("column")
        description = str(survivor.get("description", "behavioral mutation"))
        diagnostics.append(
            TargetDiagnostic(
                code="JAUNT_TS_CONTRACT_MUTANT_SURVIVED",
                message=(
                    f"The contract battery did not detect this {description}. "
                    "Strengthen the TSDoc examples/properties and reconcile again."
                ),
                path=source_path,
                line=int(line_value) if isinstance(line_value, int) else None,
                column=int(column_value) if isinstance(column_value, int) else None,
                data={"mutant_id": str(survivor.get("id", "unknown"))},
            )
        )
    return tuple(diagnostics)


async def _generate_battery(
    root: Path,
    config: JauntConfig,
    client: object,
    source: Path,
    symbol: str,
    source_text: str,
    backend: GeneratorBackend,
    *,
    max_attempts: int,
    initialized: object | None = None,
    workspace: Mapping[str, Any] | None = None,
    response_cache: ResponseCache | None = None,
    cost_tracker: CostTracker | None = None,
    progress: Callable[[str, str], None] | None = None,
    builtin_skill_names: Sequence[str] | None = None,
) -> tuple[Path, str, TokenUsage | None, tuple[str, ...]]:
    battery = _battery_path(root, config, source, symbol)
    projected = await _project_contract(client, root, source, symbol, source_text)
    declaration_context = _declaration_only_contract(source_text, symbol, projected)
    request = _battery_request(
        root,
        config,
        source,
        symbol,
        battery,
        source_text,
        declaration_context=declaration_context,
        builtin_skill_names=builtin_skill_names,
    )
    if workspace is not None:
        battery_relative = battery.relative_to(root).as_posix()
        owner = _owner_project_for_source(
            root,
            config,
            workspace,
            source.relative_to(root).as_posix(),
        )
        property_count = request.cache_payload.get("propertyCount")
        _validate_test_owner_dependencies(
            root,
            workspace,
            {owner: (battery_relative,)},
            require_fast_check=isinstance(property_count, int) and property_count > 0,
        )
    source_digest = _sha256(source_text.encode("utf-8"))
    base_validator = request.validator

    async def validate_candidate(candidate: str) -> list[str]:
        static_errors = base_validator(candidate)
        if inspect.isawaitable(static_errors):
            static_errors = await static_errors
        if static_errors:
            return list(static_errors)
        if workspace is None:
            return []
        property_block = request.cache_payload.get("propertyBlock", "")
        rendered_source = attach_property_block(
            candidate,
            property_block if isinstance(property_block, str) else "",
        )
        rendered = _with_header(
            rendered_source,
            source.relative_to(root).as_posix(),
            source_digest,
            _sha256((property_block if isinstance(property_block, str) else "").encode("utf-8")),
        )
        battery_relative = battery.relative_to(root).as_posix()
        owner = _owner_project_for_source(
            root,
            config,
            workspace,
            source.relative_to(root).as_posix(),
        )
        checked = await _run_test_batches(
            client,
            root,
            config,
            workspace,
            files=(battery_relative,),
            explicit_owners={battery_relative: owner},
            overlays={
                source.relative_to(root).as_posix(): source_text,
                battery_relative: rendered,
            },
            typecheck_only=True,
        )
        if bool(checked.get("ok", False)):
            return []
        return ["generated TypeScript contract battery failed analyzer overlay typechecking"]

    request = replace(request, validator=validate_candidate)
    runner_fingerprint = (
        _runner_fingerprint(root, client, initialized)
        if initialized is not None
        else "runner-unavailable"
    )
    fingerprint = _contract_generation_fingerprint(root, request, runner_fingerprint)
    result = await generate_request_cached(
        backend,
        request,
        max_attempts=max_attempts,
        generation_fingerprint=fingerprint,
        response_cache=response_cache,
        cost_tracker=cost_tracker,
        progress=progress,
    )
    if result.usage is not None and cost_tracker is not None:
        cost_tracker.record(f"{source.relative_to(root).as_posix()}#{symbol}", result.usage)
        cost_tracker.check_budget()
    if result.source is None or result.errors:
        raise JauntGenerationError(
            "Could not derive the TypeScript contract battery: "
            + "; ".join(result.errors or ["model returned no source"])
        )
    property_block = request.cache_payload.get("propertyBlock", "")
    rendered_source = attach_property_block(
        result.source,
        property_block if isinstance(property_block, str) else "",
    )
    rendered = _with_header(
        rendered_source,
        source.relative_to(root).as_posix(),
        source_digest,
        _sha256((property_block if isinstance(property_block, str) else "").encode("utf-8")),
    )
    return battery, rendered, result.usage, result.advisories


def _contract_generation_fingerprint(
    root: Path,
    request: GenerationRequest,
    runner_fingerprint: str,
) -> str:
    """Fingerprint every runtime and seeded-skill input to contract generation."""

    return _sha256(
        json.dumps(
            {
                "kind": "contract-test",
                "runner": runner_fingerprint,
                "propertyRendererScheme": PROPERTY_RENDERER_SCHEME,
                "propertyBlockDigest": _sha256(
                    str(request.cache_payload.get("propertyBlock", "")).encode("utf-8")
                ),
                "prompt": _sha256(request.prompt.encode("utf-8")),
                "builtinSkills": tuple(request.builtin_skill_names),
                "skillsFingerprint": skills_fingerprint(
                    project_root=root,
                    builtin_names=request.builtin_skill_names,
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


async def run_adopt(
    root: Path,
    config: JauntConfig,
    *,
    target: str,
    apply: bool = True,
    generator: GeneratorBackend | None = None,
    cost_tracker: CostTracker | None = None,
    response_cache: ResponseCache | None = None,
    progress: object | None = None,
    worker_factory: WorkerFactory | None = None,
    max_attempts: int = 2,
    auto_skills_enabled: bool | None = None,
    builtin_skill_names: Sequence[str] | None = None,
) -> LifecycleReport:
    """Mark existing TypeScript code as a contract and derive its first battery."""

    root = root.resolve()
    if response_cache is None:
        response_cache = ResponseCache(root / ".jaunt" / "cache")
    source, symbol = _split_target(root, target)
    original = _read_exact(source)
    backend = generator or _default_backend(config)
    cost = cost_tracker or CostTracker(max_cost=config.llm.max_cost_per_build)
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    npm_skill_metadata: Mapping[str, object] = {}
    use_auto_skills = (
        bool(config.skills.auto) if auto_skills_enabled is None else auto_skills_enabled
    )
    if use_auto_skills:
        from jaunt.skills_npm import ensure_npm_skills, typescript_package_owners

        npm_skills = ensure_npm_skills(
            project_root=root,
            package_owners=typescript_package_owners(root, _target(config)),
            max_readme_chars=config.skills.max_chars_per_skill,
        )
        npm_skill_metadata = npm_skills.metadata()
    expected_battery = _battery_path(root, config, source, symbol)
    battery_precondition = _precondition(expected_battery)
    strength_report: Mapping[str, Any] | None = None
    strength_diagnostics: tuple[TargetDiagnostic, ...] = ()
    async with worker_session(root, config, worker_factory=worker_factory) as (
        client,
        initialized,
    ):
        source_projection = await _project_contract(client, root, source, symbol, original)
        marked = _add_contract_tag(original, symbol, source_projection)
        if marked == original:
            raise JauntConfigError(f"{target} is already marked with {_CONTRACT_TAG}")
        analysis = await analyze(client, initialized)
        _progress_phase(progress, target, "generating contract battery")
        battery, battery_source, _usage, _advisories = await _generate_battery(
            root,
            config,
            client,
            source,
            symbol,
            marked,
            backend,
            max_attempts=max_attempts,
            initialized=initialized,
            workspace=analysis.workspace,
            response_cache=response_cache,
            cost_tracker=cost,
            progress=lambda stage, detail: _progress_phase(progress, target, stage, detail),
            builtin_skill_names=effective_builtin_skills,
        )
        overlay = {
            source.relative_to(root).as_posix(): marked,
            battery.relative_to(root).as_posix(): battery_source,
        }
        battery_relative = battery.relative_to(root).as_posix()
        owner = _owner_project_for_source(
            root,
            config,
            analysis.workspace,
            source.relative_to(root).as_posix(),
        )
        # _generate_battery validates both fresh and cached candidates through the
        # analyzer overlay. The lifecycle gate here executes that validated battery.
        checked = await _run_test_batches(
            client,
            root,
            config,
            analysis.workspace,
            files=(battery_relative,),
            explicit_owners={battery_relative: owner},
            overlays=overlay,
        )
        if bool(checked.get("ok", False)) and config.contract.strength:
            strength_report = await _run_mutation_strength(
                client,
                root,
                config,
                source_path=source.relative_to(root).as_posix(),
                symbol=symbol,
                battery_file=battery_relative,
                owner_project=owner,
                overlays=overlay,
            )
            strength_diagnostics = _mutation_strength_diagnostics(
                strength_report, source.relative_to(root).as_posix()
            )
            battery_source = _with_strength_metadata(battery_source, strength_report)
    if not bool(checked.get("ok", False)):
        _progress_advance(progress, target, ok=False)
        _progress_finish(progress)
        return LifecycleReport(
            command="adopt",
            targets=(target,),
            diagnostics=(
                TargetDiagnostic(
                    code="JAUNT_TS_CONTRACT_TYPECHECK",
                    message="The proposed contract battery did not typecheck or pass.",
                ),
            ),
            usage=cost.summary_dict(),
            metadata={
                **({"npm_skills": npm_skill_metadata} if npm_skill_metadata else {}),
                **(
                    {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
                    if response_cache is not None
                    else {}
                ),
            },
            exit_code=4,
        )
    if strength_diagnostics:
        _progress_advance(progress, target, ok=False)
        _progress_finish(progress)
        return LifecycleReport(
            command="adopt",
            targets=(target,),
            diagnostics=strength_diagnostics,
            usage=cost.summary_dict(),
            metadata={
                **({"npm_skills": npm_skill_metadata} if npm_skill_metadata else {}),
                **(
                    {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
                    if response_cache is not None
                    else {}
                ),
                "strength": {
                    "enabled": True,
                    "scheme": _MUTATION_SCHEME,
                    "targets": {target: strength_report},
                },
            },
            exit_code=4,
        )
    proposed = {
        source.relative_to(root).as_posix(): marked,
        battery.relative_to(root).as_posix(): battery_source,
    }
    if not apply:
        _progress_advance(progress, target, ok=True)
        _progress_finish(progress)
        return LifecycleReport(
            command="adopt",
            targets=(target,),
            proposed=proposed,
            usage=cost.summary_dict(),
            metadata={
                **({"npm_skills": npm_skill_metadata} if npm_skill_metadata else {}),
                **(
                    {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
                    if response_cache is not None
                    else {}
                ),
                "strength": {
                    "enabled": config.contract.strength,
                    **(
                        {
                            "scheme": _MUTATION_SCHEME,
                            "targets": {target: strength_report},
                        }
                        if strength_report is not None
                        else {}
                    ),
                },
            },
        )
    atomic_write_manifest(
        root,
        tuple(
            _Write(path=path, content=content, kind="contract", module_id=target)
            for path, content in proposed.items()
        ),
        expected_inputs={
            source.relative_to(root).as_posix(): _sha256(original.encode("utf-8")),
            expected_battery.relative_to(root).as_posix(): battery_precondition,
        },
    )
    append_events(root, [JournalEvent("adopt", target, battery.relative_to(root).as_posix())])
    _progress_advance(progress, target, ok=True)
    _progress_finish(progress)
    return LifecycleReport(
        command="adopt",
        targets=(target,),
        changed=tuple(proposed),
        usage=cost.summary_dict(),
        metadata={
            **({"npm_skills": npm_skill_metadata} if npm_skill_metadata else {}),
            **(
                {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
                if response_cache is not None
                else {}
            ),
            "strength": {
                "enabled": config.contract.strength,
                **(
                    {"scheme": _MUTATION_SCHEME, "targets": {target: strength_report}}
                    if strength_report is not None
                    else {}
                ),
            },
        },
    )


def _contract_records(analysis: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = analysis.get("contracts", [])
    return tuple(item for item in raw if isinstance(item, Mapping)) if isinstance(raw, list) else ()


async def run_reconcile(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    generator: GeneratorBackend | None = None,
    cost_tracker: CostTracker | None = None,
    response_cache: ResponseCache | None = None,
    progress: object | None = None,
    worker_factory: WorkerFactory | None = None,
    max_attempts: int = 2,
    auto_skills_enabled: bool | None = None,
    builtin_skill_names: Sequence[str] | None = None,
) -> LifecycleReport:
    """Refresh every selected committed TypeScript contract battery atomically."""

    root = root.resolve()
    if response_cache is None:
        response_cache = ResponseCache(root / ".jaunt" / "cache")
    backend = generator or _default_backend(config)
    cost = cost_tracker or CostTracker(max_cost=config.llm.max_cost_per_build)
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    npm_skill_metadata: Mapping[str, object] = {}
    use_auto_skills = (
        bool(config.skills.auto) if auto_skills_enabled is None else auto_skills_enabled
    )
    if use_auto_skills:
        from jaunt.skills_npm import ensure_npm_skills, typescript_package_owners

        npm_skills = ensure_npm_skills(
            project_root=root,
            package_owners=typescript_package_owners(root, _target(config)),
            max_readme_chars=config.skills.max_chars_per_skill,
        )
        npm_skill_metadata = npm_skills.metadata()
    proposed: dict[str, str] = {}
    battery_owners: dict[str, str] = {}
    expected_sources: dict[str, str] = {}
    targets: list[str] = []
    strength_work: list[tuple[str, str, str, str, str]] = []
    strength_reports: dict[str, Mapping[str, Any]] = {}
    async with worker_session(root, config, worker_factory=worker_factory) as (client, initialized):
        analysis = await analyze(client, initialized)
        records = _contract_records(analysis.workspace)
        for record in records:
            path_value = record.get("path")
            symbols = record.get("symbols", [])
            if not isinstance(path_value, str) or not isinstance(symbols, list):
                continue
            source = _safe_path(root, path_value)
            source_text = _read_exact(source)
            owner = _owner_project_for_source(root, config, analysis.workspace, path_value)
            expected_sources[path_value] = _sha256(source_text.encode("utf-8"))
            for symbol_value in symbols:
                symbol = (
                    str(symbol_value.get("name"))
                    if isinstance(symbol_value, Mapping)
                    else str(symbol_value)
                )
                target = f"{path_value}#{symbol}"
                qualified = f"ts:{Path(path_value).with_suffix('').as_posix()}#{symbol}"
                if target_ids and not {target, qualified}.intersection(target_ids):
                    continue
                expected_battery = _battery_path(root, config, source, symbol)
                expected_sources[expected_battery.relative_to(root).as_posix()] = _precondition(
                    expected_battery
                )
                _progress_phase(progress, target, "generating contract battery")
                battery, rendered, _usage, _advisories = await _generate_battery(
                    root,
                    config,
                    client,
                    source,
                    symbol,
                    source_text,
                    backend,
                    max_attempts=max_attempts,
                    initialized=initialized,
                    workspace=analysis.workspace,
                    response_cache=response_cache,
                    cost_tracker=cost,
                    progress=lambda stage, detail, item=target: _progress_phase(
                        progress, item, stage, detail
                    ),
                    builtin_skill_names=effective_builtin_skills,
                )
                battery_relative = battery.relative_to(root).as_posix()
                proposed[battery_relative] = rendered
                battery_owners[battery_relative] = owner
                targets.append(target)
                strength_work.append((target, path_value, symbol, battery_relative, owner))
                _progress_advance(progress, target, ok=True)
        if proposed:
            checked = await _run_test_batches(
                client,
                root,
                config,
                analysis.workspace,
                files=tuple(proposed),
                explicit_owners=battery_owners,
                overlays=proposed,
                typecheck_only=True,
            )
            if not bool(checked.get("ok", False)):
                _progress_finish(progress)
                return LifecycleReport(
                    command="reconcile",
                    targets=tuple(targets),
                    diagnostics=(
                        TargetDiagnostic(
                            code="JAUNT_TS_CONTRACT_TYPECHECK",
                            message="Refreshed contract batteries did not typecheck.",
                        ),
                    ),
                    usage=cost.summary_dict(),
                    metadata={
                        **({"npm_skills": npm_skill_metadata} if npm_skill_metadata else {}),
                        **(
                            {
                                "cache": {
                                    "hits": response_cache.hits,
                                    "misses": response_cache.misses,
                                }
                            }
                            if response_cache is not None
                            else {}
                        ),
                    },
                    exit_code=4,
                )
            run = await _run_test_batches(
                client,
                root,
                config,
                analysis.workspace,
                files=tuple(proposed),
                explicit_owners=battery_owners,
                overlays=proposed,
            )
            if not bool(run.get("ok", False)):
                _progress_finish(progress)
                return LifecycleReport(
                    command="reconcile",
                    targets=tuple(targets),
                    diagnostics=(
                        TargetDiagnostic(
                            code="JAUNT_TS_CONTRACT_FAILED",
                            message="Refreshed contract batteries did not pass.",
                        ),
                    ),
                    usage=cost.summary_dict(),
                    metadata={
                        **({"npm_skills": npm_skill_metadata} if npm_skill_metadata else {}),
                        **(
                            {
                                "cache": {
                                    "hits": response_cache.hits,
                                    "misses": response_cache.misses,
                                }
                            }
                            if response_cache is not None
                            else {}
                        ),
                    },
                    exit_code=4,
                )
            if config.contract.strength:
                strength_diagnostics: list[TargetDiagnostic] = []
                for target, source_path, symbol, battery_relative, owner in strength_work:
                    report = await _run_mutation_strength(
                        client,
                        root,
                        config,
                        source_path=source_path,
                        symbol=symbol,
                        battery_file=battery_relative,
                        owner_project=owner,
                        overlays=proposed,
                    )
                    strength_reports[target] = report
                    strength_diagnostics.extend(_mutation_strength_diagnostics(report, source_path))
                    proposed[battery_relative] = _with_strength_metadata(
                        proposed[battery_relative], report
                    )
                if strength_diagnostics:
                    _progress_finish(progress)
                    return LifecycleReport(
                        command="reconcile",
                        targets=tuple(targets),
                        diagnostics=tuple(strength_diagnostics),
                        usage=cost.summary_dict(),
                        metadata={
                            **({"npm_skills": npm_skill_metadata} if npm_skill_metadata else {}),
                            **(
                                {
                                    "cache": {
                                        "hits": response_cache.hits,
                                        "misses": response_cache.misses,
                                    }
                                }
                                if response_cache is not None
                                else {}
                            ),
                            "strength": {
                                "enabled": True,
                                "scheme": _MUTATION_SCHEME,
                                "targets": strength_reports,
                            },
                        },
                        exit_code=4,
                    )
    atomic_write_manifest(
        root,
        tuple(
            _Write(path=path, content=content, kind="contract", module_id="ts-contract")
            for path, content in proposed.items()
        ),
        expected_inputs=expected_sources,
    )
    append_events(
        root, [JournalEvent("reconcile", target, "TypeScript contract") for target in targets]
    )
    _progress_finish(progress)
    return LifecycleReport(
        command="reconcile",
        targets=tuple(targets),
        changed=tuple(proposed),
        usage=cost.summary_dict(),
        metadata={
            **({"npm_skills": npm_skill_metadata} if npm_skill_metadata else {}),
            **(
                {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
                if response_cache is not None
                else {}
            ),
            "strength": {
                "enabled": config.contract.strength,
                **(
                    {"scheme": _MUTATION_SCHEME, "targets": strength_reports}
                    if strength_reports
                    else {}
                ),
            },
        },
    )


def _strip_battery_header(source: str) -> str:
    return _strip_managed_battery_header(source)


def _render_type_docs(text: str) -> str:
    """Match the worker's deterministic TSDoc rendering for an IR declaration."""

    if not text:
        return ""
    lines = text.replace("*/", "* /").splitlines()
    body = "\n".join(f" * {line}".rstrip() for line in lines)
    return f"/**\n{body}\n */\n"


@dataclass(frozen=True, slots=True)
class _TsLexeme:
    """One significant TypeScript token from the conservative eject lexer."""

    kind: str
    text: str
    start: int
    end: int
    brace_depth: int
    template_depth: int


@dataclass(frozen=True, slots=True)
class _TsIgnoredSpan:
    """A lexical region where identifier-looking text is data, not code."""

    kind: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _TsLexicalView:
    tokens: tuple[_TsLexeme, ...]
    ignored: tuple[_TsIgnoredSpan, ...]


_TS_REGEX_PREFIX_KEYWORDS = frozenset(
    {
        "await",
        "case",
        "delete",
        "do",
        "else",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }
)
_TS_PUNCTUATORS: tuple[str, ...] = (
    ">>>=",
    "===",
    "!==",
    "**=",
    "&&=",
    "||=",
    "??=",
    ">>>",
    "...",
    "=>",
    "==",
    "!=",
    "<=",
    ">=",
    "++",
    "--",
    "&&",
    "||",
    "??",
    "?.",
    "**",
    "<<",
    ">>",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "&=",
    "|=",
    "^=",
)


def _is_ts_identifier_start(character: str) -> bool:
    return character in {"$", "_"} or character.isalpha() or ord(character) >= 0x80


def _is_ts_identifier_continue(character: str) -> bool:
    return (
        _is_ts_identifier_start(character)
        or character.isdigit()
        or character
        in {
            "\u200c",
            "\u200d",
        }
    )


class _TypeScriptEjectLexer:
    """Small fail-closed lexer for source-preserving magic ejection.

    This is deliberately not a TypeScript parser.  It only distinguishes code
    identifiers from comments and literal payloads so reserved bindings can be
    renamed without changing user-visible data.  Template expressions are
    scanned as code; malformed strings, comments, regexes, and templates abort
    ejection before the atomic write.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens: list[_TsLexeme] = []
        self.ignored: list[_TsIgnoredSpan] = []

    def scan(self) -> _TsLexicalView:
        end = self._scan_code(0, template_depth=0, stop_at_template_close=False)
        if end != len(self.source):  # pragma: no cover - recursive invariant
            raise JauntConfigError("Cannot safely eject malformed TypeScript source")
        return _TsLexicalView(tuple(self.tokens), tuple(self.ignored))

    def _token(
        self,
        kind: str,
        start: int,
        end: int,
        *,
        brace_depth: int,
        template_depth: int,
    ) -> None:
        self.tokens.append(
            _TsLexeme(
                kind,
                self.source[start:end],
                start,
                end,
                brace_depth,
                template_depth,
            )
        )

    def _quoted(self, start: int, quote: str) -> int:
        index = start + 1
        while index < len(self.source):
            character = self.source[index]
            if character == "\\":
                index += 2
                continue
            if character == quote:
                return index + 1
            if character in {"\n", "\r"}:
                break
            index += 1
        raise JauntConfigError("Cannot safely eject TypeScript with an unterminated string")

    def _regex(self, start: int) -> int:
        index = start + 1
        in_class = False
        while index < len(self.source):
            character = self.source[index]
            if character == "\\":
                index += 2
                continue
            if character in {"\n", "\r"}:
                break
            if character == "[":
                in_class = True
            elif character == "]":
                in_class = False
            elif character == "/" and not in_class:
                index += 1
                while index < len(self.source) and _is_ts_identifier_continue(self.source[index]):
                    index += 1
                return index
            index += 1
        raise JauntConfigError(
            "Cannot safely eject TypeScript with an unterminated regular expression"
        )

    def _template(self, start: int, *, template_depth: int) -> int:
        raw_start = start
        index = start + 1
        while index < len(self.source):
            character = self.source[index]
            if character == "\\":
                index += 2
                continue
            if character == "`":
                self.ignored.append(_TsIgnoredSpan("template", raw_start, index + 1))
                return index + 1
            if character == "$" and index + 1 < len(self.source) and self.source[index + 1] == "{":
                self.ignored.append(_TsIgnoredSpan("template", raw_start, index + 2))
                index = self._scan_code(
                    index + 2,
                    template_depth=template_depth + 1,
                    stop_at_template_close=True,
                )
                raw_start = index - 1
                continue
            index += 1
        raise JauntConfigError("Cannot safely eject TypeScript with an unterminated template")

    def _scan_code(
        self,
        start: int,
        *,
        template_depth: int,
        stop_at_template_close: bool,
    ) -> int:
        index = start
        brace_depth = 0
        regex_allowed = True
        while index < len(self.source):
            character = self.source[index]
            if character.isspace():
                index += 1
                continue
            if character == "}" and stop_at_template_close and brace_depth == 0:
                return index + 1
            if self.source.startswith("//", index):
                end = self.source.find("\n", index + 2)
                end = len(self.source) if end < 0 else end
                self.ignored.append(_TsIgnoredSpan("comment", index, end))
                index = end
                continue
            if self.source.startswith("/*", index):
                end = self.source.find("*/", index + 2)
                if end < 0:
                    raise JauntConfigError(
                        "Cannot safely eject TypeScript with an unterminated block comment"
                    )
                end += 2
                self.ignored.append(_TsIgnoredSpan("comment", index, end))
                index = end
                continue
            if character in {"'", '"'}:
                end = self._quoted(index, character)
                self.ignored.append(_TsIgnoredSpan("string", index, end))
                index = end
                regex_allowed = False
                continue
            if character == "`":
                index = self._template(index, template_depth=template_depth)
                regex_allowed = False
                continue
            if character == "/" and regex_allowed:
                end = self._regex(index)
                self.ignored.append(_TsIgnoredSpan("regex", index, end))
                index = end
                regex_allowed = False
                continue
            if _is_ts_identifier_start(character):
                end = index + 1
                while end < len(self.source) and _is_ts_identifier_continue(self.source[end]):
                    end += 1
                self._token(
                    "identifier",
                    index,
                    end,
                    brace_depth=brace_depth,
                    template_depth=template_depth,
                )
                regex_allowed = self.source[index:end] in _TS_REGEX_PREFIX_KEYWORDS
                index = end
                continue
            if character.isdigit() or (
                character == "."
                and index + 1 < len(self.source)
                and self.source[index + 1].isdigit()
            ):
                end = index + 1
                while end < len(self.source) and (
                    self.source[end].isalnum() or self.source[end] in {"_", "."}
                ):
                    end += 1
                self._token(
                    "number",
                    index,
                    end,
                    brace_depth=brace_depth,
                    template_depth=template_depth,
                )
                regex_allowed = False
                index = end
                continue
            punctuator = next(
                (
                    candidate
                    for candidate in _TS_PUNCTUATORS
                    if self.source.startswith(candidate, index)
                ),
                character,
            )
            end = index + len(punctuator)
            self._token(
                "punctuator",
                index,
                end,
                brace_depth=brace_depth,
                template_depth=template_depth,
            )
            if punctuator == "{":
                brace_depth += 1
            elif punctuator == "}":
                if brace_depth == 0:
                    raise JauntConfigError("Cannot safely eject unbalanced TypeScript braces")
                brace_depth -= 1
            regex_allowed = punctuator not in {
                ")",
                "]",
                "}",
                "++",
                "--",
                ".",
                "?.",
            }
            index = end
        if stop_at_template_close:
            raise JauntConfigError(
                "Cannot safely eject TypeScript with an unterminated template expression"
            )
        if brace_depth:
            raise JauntConfigError("Cannot safely eject unbalanced TypeScript braces")
        return index


def _typescript_lexical_view(source: str) -> _TsLexicalView:
    return _TypeScriptEjectLexer(source).scan()


def _span_contains(spans: Sequence[_TsIgnoredSpan], position: int) -> bool:
    return any(span.start <= position < span.end for span in spans)


def _typescript_code_matches(source: str, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    view = _typescript_lexical_view(source)
    return [
        match
        for match in pattern.finditer(source)
        if not _span_contains(view.ignored, match.start())
    ]


def _typescript_code_sub(source: str, pattern: re.Pattern[str], replacement: str) -> str:
    matches = _typescript_code_matches(source, pattern)
    for match in reversed(matches):
        source = source[: match.start()] + replacement + source[match.end() :]
    return source


def _rename_typescript_identifiers(source: str, replacements: Mapping[str, str]) -> str:
    view = _typescript_lexical_view(source)
    edits = [
        (token.start, token.end, replacements[token.text])
        for token in view.tokens
        if token.kind == "identifier" and token.text in replacements
    ]
    for start, end, replacement in reversed(edits):
        source = source[:start] + replacement + source[end:]
    return source


def _rename_reserved_typescript_binding(source: str, private: str, public: str) -> str:
    """Rename one generated binding while refusing property-name ambiguities."""

    declarations = _typescript_top_level_declarations(source, private)
    if len(declarations) != 1:
        raise JauntConfigError(
            f"Cannot safely eject {public!r}: expected one reserved declaration, "
            f"found {len(declarations)}"
        )
    declaration = declarations[0]
    view = _typescript_lexical_view(source)
    private_tokens = [
        (index, token)
        for index, token in enumerate(view.tokens)
        if token.kind == "identifier" and token.text == private
    ]
    if any(token.kind == "identifier" and token.text == public for token in view.tokens):
        raise JauntConfigError(
            f"Cannot safely eject {public!r}: the public name already appears in generated "
            "code and could capture a renamed binding"
        )
    declaration_tokens = [
        token
        for _, token in private_tokens
        if token.template_depth == 0
        and token.brace_depth == 0
        and token.start > declaration.keyword_start
    ]
    if not declaration_tokens:
        raise JauntConfigError(
            f"Cannot safely eject {public!r}: reserved declaration name is ambiguous"
        )
    declaration_name = declaration_tokens[0]
    for index, token in private_tokens:
        if token.start == declaration_name.start:
            continue
        previous = view.tokens[index - 1].text if index else ""
        following = view.tokens[index + 1].text if index + 1 < len(view.tokens) else ""
        property_position = (
            previous in {".", "?."}
            or following == ":"
            or (
                following == "?"
                and index + 2 < len(view.tokens)
                and view.tokens[index + 2].text == ":"
            )
        )
        shorthand_or_member = previous in {"{", ","} and following in {
            ",",
            "}",
            "=",
            "(",
        }
        if property_position or shorthand_or_member:
            raise JauntConfigError(
                f"Cannot safely eject {public!r}: reserved name {private!r} appears in a "
                "non-binding property position"
            )
    return _rename_typescript_identifiers(source, {private: public})


def _typescript_private_code_markers(source: str) -> tuple[str, ...]:
    """Return Jaunt-private spellings that occur in code, not literal data."""

    view = _typescript_lexical_view(source)
    markers = {
        token.text
        for token in view.tokens
        if token.kind == "identifier"
        and ("__jaunt_impl_" in token.text or "__JauntApi" in token.text)
    }
    # Escaped or otherwise unsupported identifier spellings must fail closed.
    for needle in ("__jaunt_impl_", "__JauntApi"):
        start = 0
        while (position := source.find(needle, start)) >= 0:
            if not _span_contains(view.ignored, position):
                markers.add(needle)
            start = position + len(needle)
    return tuple(sorted(markers))


@dataclass(frozen=True, slots=True)
class _TsDeclaration:
    line_start: int
    keyword_start: int


def _typescript_top_level_declarations(source: str, name: str) -> tuple[_TsDeclaration, ...]:
    tokens = [
        token
        for token in _typescript_lexical_view(source).tokens
        if token.template_depth == 0 and token.brace_depth == 0
    ]
    declarations: list[_TsDeclaration] = []
    for index, token in enumerate(tokens):
        keyword_start: int | None = None
        name_token: _TsLexeme | None = None
        if token.text in {"function", "class", "const", "let", "var"}:
            keyword_start = token.start
            if index + 1 < len(tokens):
                name_token = tokens[index + 1]
        elif (
            token.text == "async"
            and index + 2 < len(tokens)
            and tokens[index + 1].text == "function"
        ):
            keyword_start = token.start
            name_token = tokens[index + 2]
        if (
            keyword_start is None
            or name_token is None
            or name_token.kind != "identifier"
            or name_token.text != name
        ):
            continue
        line_start = source.rfind("\n", 0, keyword_start) + 1
        if source[line_start:keyword_start].strip():
            continue
        declarations.append(_TsDeclaration(line_start, keyword_start))
    return tuple(declarations)


def _strip_leading_generated_header(source: str) -> str:
    pattern = re.compile(r"\A//\s*(?:⚙️\s*|⛓️\s*)?jaunt:[^\r\n]*(?:\r?\n|\Z)")
    while match := pattern.match(source):
        source = source[match.end() :]
    return source


def _standalone_api_types(api_source: str, type_declarations: object) -> str:
    """Render AST-bounded interface/type declarations for an ejected module.

    The worker obtains ``source`` from a TypeScript declaration node.  Using that
    source avoids trying to lex semicolons or braces inside object, function, and
    template-literal types in Python.  The API-mirror check ties the IR record to
    another independently rendered worker artifact and fails closed on malformed
    or inconsistent worker output.
    """

    if not isinstance(type_declarations, list):
        raise JauntConfigError(
            "The worker omitted AST-bounded type declarations required for magic ejection"
        )
    declarations: list[str] = []
    for index, raw in enumerate(type_declarations):
        if not isinstance(raw, Mapping):
            raise JauntConfigError(
                f"The worker returned malformed type declaration {index} for magic ejection"
            )
        declaration_record = cast("Mapping[str, object]", raw)
        kind = declaration_record.get("kind")
        name = declaration_record.get("name")
        source = declaration_record.get("source")
        docs = declaration_record.get("docs", "")
        if (
            kind not in {"interface", "type"}
            or not isinstance(name, str)
            or not name
            or not isinstance(source, str)
            or not source.strip()
            or not isinstance(docs, str)
        ):
            raise JauntConfigError(
                f"The worker returned malformed type declaration {index} for magic ejection"
            )
        declaration = source.strip()
        prefix = re.compile(rf"\Aexport\s+(?:declare\s+)?{kind}\s+{re.escape(name)}(?![\w$])")
        if not prefix.match(declaration) or declaration not in api_source:
            raise JauntConfigError(
                f"The worker returned inconsistent {kind} declaration {name!r} for magic ejection"
            )
        declarations.append(f"{_render_type_docs(docs)}{declaration}")
    return "\n\n".join(declarations)


def _type_import_bindings(item: Mapping[str, object], index: int) -> tuple[str, ...]:
    bindings: list[str] = []
    for key in ("defaultImport", "namespaceImport"):
        value = item.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_$][\w$]*", value):
            raise JauntConfigError(f"The worker returned malformed {key} in type import {index}")
        bindings.append(value)
    named = item.get("namedImports")
    if not isinstance(named, list):
        raise JauntConfigError(f"The worker returned malformed namedImports in type import {index}")
    for binding_index, raw_binding in enumerate(named):
        if not isinstance(raw_binding, Mapping):
            raise JauntConfigError(
                f"The worker returned malformed binding {binding_index} in type import {index}"
            )
        binding = cast("Mapping[str, object]", raw_binding)
        imported = binding.get("imported")
        local = binding.get("local")
        type_only = binding.get("typeOnly")
        if (
            not isinstance(imported, str)
            or not re.fullmatch(r"[A-Za-z_$][\w$]*", imported)
            or not isinstance(local, str)
            or not re.fullmatch(r"[A-Za-z_$][\w$]*", local)
            or not isinstance(type_only, bool)
        ):
            raise JauntConfigError(
                f"The worker returned malformed binding {binding_index} in type import {index}"
            )
        bindings.append(local)
    if not bindings:
        raise JauntConfigError(f"The worker returned empty type import {index}")
    if len(bindings) != len(set(bindings)):
        raise JauntConfigError(f"The worker returned duplicate bindings in type import {index}")
    return tuple(bindings)


def _render_structured_type_import(item: Mapping[str, object], index: int) -> str:
    specifier = item.get("specifier")
    type_only = item.get("typeOnly")
    runtime = item.get("runtime")
    if (
        not isinstance(specifier, str)
        or not specifier
        or not isinstance(type_only, bool)
        or not isinstance(runtime, bool)
    ):
        raise JauntConfigError(f"The worker returned malformed type import {index}")
    _type_import_bindings(item, index)
    parts: list[str] = []
    default_import = item.get("defaultImport")
    namespace_import = item.get("namespaceImport")
    if isinstance(default_import, str):
        parts.append(default_import)
    if isinstance(namespace_import, str):
        parts.append(f"* as {namespace_import}")
    named = cast("list[Mapping[str, object]]", item["namedImports"])
    if named:
        rendered: list[str] = []
        for binding in named:
            imported = str(binding["imported"])
            local = str(binding["local"])
            binding_prefix = "type " if not type_only and binding["typeOnly"] else ""
            rendered.append(
                f"{binding_prefix}{imported if imported == local else f'{imported} as {local}'}"
            )
        parts.append(f"{{ {', '.join(rendered)} }}")
    prefix = "" if runtime and not type_only else "type "
    return f"import {prefix}{', '.join(parts)} from {json.dumps(specifier, ensure_ascii=False)};"


def _standalone_api_type_imports(
    module: Mapping[str, Any], api_source: str, implementation_body: str
) -> str:
    """Keep API-relevant type imports when the generated mirror is removed."""

    raw_imports = module.get("typeImports")
    if not isinstance(raw_imports, list):
        raise JauntConfigError(
            "The worker omitted structured type imports required for magic ejection"
        )
    api_tokens = _typescript_lexical_view(api_source).tokens
    imports: list[str] = []
    for index, raw_item in enumerate(raw_imports):
        if not isinstance(raw_item, Mapping):
            raise JauntConfigError(f"The worker returned malformed type import {index}")
        item = cast("Mapping[str, object]", raw_item)
        rendered = _render_structured_type_import(item, index)
        if rendered not in api_source:
            raise JauntConfigError(
                f"The worker returned type import {index} inconsistent with the API mirror"
            )
        bindings = _type_import_bindings(item, index)
        # Every binding occurs once in its import declaration. A second API
        # token proves that a public declaration actually consumes it, avoiding
        # noUnusedLocals failures from carrying irrelevant spec imports forward.
        used = any(
            sum(token.kind == "identifier" and token.text == binding for token in api_tokens) > 1
            for binding in bindings
        )
        if not used:
            continue
        if item["runtime"]:
            # Runtime imports are injected deterministically into the composed
            # implementation and are moved with that body below.
            if not any(
                token.kind == "identifier" and token.text == binding
                for token in _typescript_lexical_view(implementation_body).tokens
                for binding in bindings
            ):
                raise JauntConfigError(f"Magic ejection lost required runtime type import {index}")
            continue
        imports.append(
            _retarget_relative_imports(
                rendered,
                _module_path_for_eject(module, "apiMirrorPath"),
                _module_path_for_eject(module, "facadePath"),
            )
        )
    return "\n".join(imports)


def _render_indented_type_docs(text: str, indent: str) -> str:
    return "".join(f"{indent}{line}" for line in _render_type_docs(text).splitlines(keepends=True))


def _class_member_kind(tokens: Sequence[_TsLexeme], index: int, class_end: int) -> str:
    token = tokens[index]
    previous = tokens[index - 1].text if index else ""
    if token.text == "constructor":
        return "constructor"
    # ``get`` and ``set`` are ordinary identifier names when immediately
    # followed by ``(``, but are accessor keywords when followed by the real
    # member name.  Do not mistake the keyword token in ``get size()`` for a
    # method literally named ``get``; a class may legally contain both.
    if (
        token.text in {"get", "set"}
        and index + 1 < len(tokens)
        and tokens[index + 1].kind == "identifier"
        and tokens[index + 1].brace_depth == token.brace_depth
    ):
        return "accessor-keyword"
    if previous == "get":
        return "getter"
    if previous == "set":
        return "setter"
    for candidate in tokens[index + 1 :]:
        if candidate.start >= class_end or candidate.brace_depth != 1:
            break
        if candidate.text == "(":
            return "method"
        if candidate.text in {":", "=", ";"}:
            return "property"
    return "property"


def _insert_class_member_docs(source: str, symbol: Mapping[str, object], class_name: str) -> str:
    raw_members = symbol.get("members")
    if not isinstance(raw_members, list):
        raise JauntConfigError(f"The worker omitted class members required to eject {class_name!r}")
    has_docs = False
    for raw_member in raw_members:
        if not isinstance(raw_member, Mapping):
            continue
        member = cast("Mapping[str, object]", raw_member)
        docs = member.get("docs", "")
        if isinstance(docs, str) and docs:
            has_docs = True
            break
    if not has_docs:
        return source
    view = _typescript_lexical_view(source)
    tokens = list(view.tokens)
    class_indices = [
        index
        for index, token in enumerate(tokens[:-1])
        if token.template_depth == 0
        and token.brace_depth == 0
        and token.text == "class"
        and tokens[index + 1].text == class_name
    ]
    if len(class_indices) != 1:
        raise JauntConfigError(
            f"Cannot safely eject {class_name!r}: expected one generated class declaration"
        )
    opening = next(
        (
            token
            for token in tokens[class_indices[0] + 2 :]
            if token.template_depth == 0 and token.brace_depth == 0 and token.text == "{"
        ),
        None,
    )
    if opening is None:
        raise JauntConfigError(f"Cannot safely eject {class_name!r}: malformed class body")
    closing = next(
        (
            token
            for token in tokens
            if token.start > opening.start
            and token.template_depth == 0
            and token.brace_depth == 1
            and token.text == "}"
        ),
        None,
    )
    if closing is None:
        raise JauntConfigError(f"Cannot safely eject {class_name!r}: malformed class body")
    direct = [
        token
        for token in tokens
        if opening.end <= token.start < closing.start
        and token.template_depth == 0
        and token.brace_depth == 1
    ]
    edits: list[tuple[int, str]] = []
    for member_index, raw_member in enumerate(raw_members):
        if not isinstance(raw_member, Mapping):
            raise JauntConfigError(
                f"The worker returned malformed member {member_index} for {class_name!r}"
            )
        member = cast("Mapping[str, object]", raw_member)
        docs = member.get("docs", "")
        name = member.get("name")
        kind = member.get("kind")
        static = member.get("static")
        inherited = member.get("inheritedConstructor", False)
        synthetic = member.get("synthetic", False)
        if (
            not isinstance(docs, str)
            or not isinstance(name, str)
            or kind not in {"constructor", "method", "getter", "setter", "property"}
            or not isinstance(static, bool)
            or not isinstance(inherited, bool)
            or not isinstance(synthetic, bool)
        ):
            raise JauntConfigError(
                f"The worker returned malformed member {member_index} for {class_name!r}"
            )
        if not docs or inherited:
            continue
        candidates: list[_TsLexeme] = []
        for index, token in enumerate(direct):
            if token.kind != "identifier" or token.text != name:
                continue
            detected_kind = _class_member_kind(direct, index, closing.start)
            if detected_kind != kind:
                continue
            line_start = source.rfind("\n", 0, token.start) + 1
            prefix = source[line_start : token.start]
            indent_match = re.match(r"[ \t]*", prefix)
            assert indent_match is not None
            modifiers = prefix[indent_match.end() :]
            if not re.fullmatch(
                r"(?:(?:abstract|accessor|async|declare|get|override|public|readonly|set|static)\s+)*",
                modifiers,
            ):
                continue
            is_static = bool(re.search(r"\bstatic\b", prefix))
            if is_static == static:
                candidates.append(token)
        if len(candidates) != 1:
            if synthetic and not candidates:
                continue
            raise JauntConfigError(
                f"Cannot safely eject {class_name!r}: expected one generated {kind} {name!r} "
                f"for authored member documentation, found {len(candidates)}"
            )
        token = candidates[0]
        line_start = source.rfind("\n", 0, token.start) + 1
        indent_match = re.match(r"[ \t]*", source[line_start : token.start])
        assert indent_match is not None
        rendered = _render_indented_type_docs(docs, indent_match.group(0))
        if not source[:line_start].endswith(rendered):
            edits.append((line_start, rendered))
    for position, content in reversed(edits):
        source = source[:position] + content + source[position:]
    return source


_RELATIVE_IMPORT_RE = re.compile(
    r'(?P<prefix>\bfrom\s+|\bimport\s*\(\s*)(?P<quote>["\'])(?P<path>\.[^"\']+)(?P=quote)'
)


def _retarget_relative_imports(source: str, from_path: str, to_path: str) -> str:
    """Preserve relative import targets while moving a module."""

    old_directory = posixpath.dirname(from_path)
    new_directory = posixpath.dirname(to_path)

    def replace(match: re.Match[str]) -> str:
        target = posixpath.normpath(posixpath.join(old_directory, match.group("path")))
        relative = posixpath.relpath(target, new_directory)
        if not relative.startswith("."):
            relative = f"./{relative}"
        return f"{match.group('prefix')}{match.group('quote')}{relative}{match.group('quote')}"

    matches = _typescript_code_matches(source, _RELATIVE_IMPORT_RE)
    for match in reversed(matches):
        source = source[: match.start()] + replace(match) + source[match.end() :]
    return source


def _rewrite_import_target(
    source: str,
    *,
    importer_path: str,
    old_target_path: str,
    new_target_path: str,
) -> str:
    """Retarget relative imports resolving to one exact module."""

    directory = posixpath.dirname(importer_path)
    old_js = str(Path(old_target_path).with_suffix(".js")).replace("\\", "/")
    new_js = str(Path(new_target_path).with_suffix(".js")).replace("\\", "/")

    def replace(match: re.Match[str]) -> str:
        resolved = posixpath.normpath(posixpath.join(directory, match.group("path")))
        if resolved != old_js:
            return match.group(0)
        relative = posixpath.relpath(new_js, directory)
        if not relative.startswith("."):
            relative = f"./{relative}"
        return f"{match.group('prefix')}{match.group('quote')}{relative}{match.group('quote')}"

    matches = _typescript_code_matches(source, _RELATIVE_IMPORT_RE)
    for match in reversed(matches):
        source = source[: match.start()] + replace(match) + source[match.end() :]
    return source


def _configured_ts_files(root: Path, config: JauntConfig) -> tuple[Path, ...]:
    target = config.typescript_target
    assert target is not None
    files: set[Path] = set()
    for entry in (*target.source_roots, *target.test_roots):
        roots = (
            [path for path in root.glob(entry) if path.is_dir()]
            if any(character in entry for character in "*?[")
            else [_safe_path(root, entry)]
        )
        for directory in roots:
            if directory.is_dir():
                files.update(directory.rglob("*.ts"))
                files.update(directory.rglob("*.tsx"))
    return tuple(sorted(files, key=lambda path: path.as_posix()))


def _ordinary_ejected_source(module: Mapping[str, Any], implementation: str) -> str:
    """Conservatively turn a worker-composed implementation into ordinary TS."""

    api_source = module.get("apiSource")
    symbols = module.get("symbols")
    if not isinstance(api_source, str) or not isinstance(symbols, list):
        raise JauntConfigError("The worker omitted API/symbol data required for magic ejection")
    symbol_records: list[Mapping[str, object]] = []
    for symbol in symbols:
        if not isinstance(symbol, Mapping):
            raise JauntConfigError("The worker returned malformed symbol data for magic ejection")
        record = cast("Mapping[str, object]", symbol)
        if not isinstance(record.get("name"), str) or not isinstance(record.get("docs", ""), str):
            raise JauntConfigError("The worker returned malformed symbol data for magic ejection")
        symbol_records.append(record)
    body = _strip_leading_generated_header(implementation)
    body = _typescript_code_sub(
        body,
        re.compile(r'(?m)^import\s+type\s+\*\s+as\s+__JauntApi\s+from\s+["\'][^"\']+["\'];\s*\n'),
        "",
    )
    # Remove the exact deterministic public boundary before renaming its private
    # source values. Move each symbol's TSDoc onto the resulting ordinary
    # declaration instead of leaving detached comment blocks at the file tail.
    for symbol in symbol_records:
        name = str(symbol["name"])
        boundary = f"export const {name}: typeof __JauntApi.{name} = __jaunt_impl_{name};"
        boundary_matches = _typescript_code_matches(body, re.compile(re.escape(boundary)))
        if len(boundary_matches) != 1:
            raise JauntConfigError(
                f"Cannot safely eject {name!r}: expected one generated public boundary"
            )
        boundary_match = boundary_matches[0]
        start = boundary_match.start()
        docs = _render_type_docs(str(symbol.get("docs", "")))
        if docs and body[:start].endswith(docs):
            start -= len(docs)
        end = boundary_match.end()
        if body[end : end + 2] == "\r\n":
            end += 2
        elif body[end : end + 1] == "\n":
            end += 1
        body = body[:start] + body[end:]

        type_boundary = f"export type {name} = __JauntApi.{name};"
        type_matches = _typescript_code_matches(body, re.compile(re.escape(type_boundary)))
        type_count = len(type_matches)
        if symbol.get("kind") == "class":
            if type_count != 1:
                raise JauntConfigError(
                    f"Cannot safely eject {name!r}: expected one generated class type boundary"
                )
            type_match = type_matches[0]
            type_end = type_match.end()
            if body[type_end : type_end + 2] == "\r\n":
                type_end += 2
            elif body[type_end : type_end + 1] == "\n":
                type_end += 1
            body = body[: type_match.start()] + body[type_end:]
        elif type_count:
            raise JauntConfigError(
                f"Cannot safely eject {name!r}: unexpected generated type boundary"
            )
    api_stem = Path(_module_path_for_eject(module, "apiMirrorPath")).stem
    body = _typescript_code_sub(
        body,
        re.compile(
            rf'(?m)^import\s+type\s+{{[^}}]+}}\s+from\s+["\'][^"\']*{re.escape(api_stem)}\.js["\'];\s*\n'
        ),
        "",
    )
    body = _retarget_relative_imports(
        body,
        _module_path_for_eject(module, "implementationPath"),
        _module_path_for_eject(module, "facadePath"),
    )
    for symbol in symbol_records:
        name = str(symbol["name"])
        private = f"__jaunt_impl_{name}"
        body = _rename_reserved_typescript_binding(body, private, name)
        declarations = _typescript_top_level_declarations(body, name)
        if len(declarations) != 1:
            raise JauntConfigError(
                f"Cannot safely eject {name!r}: expected one reserved declaration, "
                f"found {len(declarations)}"
            )
        declaration = declarations[0]
        docs = _render_type_docs(str(symbol.get("docs", "")))
        body = (
            body[: declaration.line_start]
            + docs
            + body[declaration.line_start : declaration.keyword_start]
            + "export "
            + body[declaration.keyword_start :]
        )
        if symbol.get("kind") == "class":
            body = _insert_class_member_docs(body, symbol, name)
    if _typescript_private_code_markers(body):
        raise JauntConfigError("Magic ejection left Jaunt-private implementation bindings")
    if _typescript_code_matches(
        body,
        re.compile(r'(?:from\s+|import\s*\(\s*)["\'][^"\']*(?:__generated__|\.jaunt(?:\.|/))'),
    ):
        raise JauntConfigError("Magic ejection left an import of a Jaunt-private artifact")
    type_imports = _standalone_api_type_imports(module, api_source, body)
    sections = [
        type_imports,
        _standalone_api_types(api_source, module.get("typeDeclarations")),
        body.strip(),
    ]
    context_path = module.get("contextPath")
    if not isinstance(context_path, str):
        routes = module.get("routes")
        context_path = routes.get("contextPath") if isinstance(routes, Mapping) else None
    if isinstance(context_path, str):
        facade = Path(_module_path_for_eject(module, "facadePath"))
        context = Path(context_path)
        relative = posixpath.relpath(
            context.with_suffix(".js").as_posix(), facade.parent.as_posix()
        )
        specifier = relative if relative.startswith(".") else f"./{relative}"
        sections.append(f'export * from "{specifier}";')
    ordinary = "\n\n".join(section for section in sections if section).rstrip() + "\n"
    _validate_ordinary_ejected_text(ordinary, label="ejected implementation")
    return ordinary


def _validate_ordinary_ejected_text(source: str, *, label: str) -> None:
    forbidden = (
        (
            r"(?:\bfrom\s+|\bimport\s*(?:\(\s*)?|\brequire\s*\(\s*)"
            r'["\']@usejaunt/ts(?:/spec)?["\']',
            "Jaunt marker runtime",
        ),
        (
            r'(?:from\s+|import\s*\(\s*)["\'][^"\']*\.jaunt(?:[./][^"\']*)?["\']',
            "private spec",
        ),
        (
            r'(?:from\s+|import\s*\(\s*)["\'][^"\']*__generated__[/][^"\']*["\']',
            "generated artifact",
        ),
    )
    for pattern, description in forbidden:
        if _typescript_code_matches(source, re.compile(pattern)):
            raise JauntConfigError(f"{label} still references {description}")


def _ordinary_test_path(test_spec_path: str, tier: str) -> str:
    source = Path(test_spec_path)
    stem = re.sub(r"\.jaunt-test$", "", source.stem)
    extension = ".tsx" if source.suffix == ".tsx" else ".ts"
    return (source.parent / f"{stem}.{tier}.test{extension}").as_posix()


def _magic_test_ejection_plan(
    root: Path,
    config: JauntConfig,
    workspace: Mapping[str, Any],
    modules: Mapping[str, Mapping[str, Any]],
    target: str,
) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Convert target-owned generated batteries to ordinary colocated Vitest files."""

    raw_specs = workspace.get("testSpecs", [])
    specs: list[Mapping[str, Any]] = (
        [item for item in raw_specs if isinstance(item, Mapping)]
        if isinstance(raw_specs, list)
        else []
    )
    specs.extend(_implicit_class_test_specs(root, config, modules, explicit_specs=specs))
    writes: dict[str, str] = {}
    deletes: set[str] = set()
    owners: dict[str, str] = {}
    target_config = config.typescript_target
    if target_config is None:
        raise JauntConfigError("No [target.ts] is configured")
    generated_dir = target_config.generated_dir
    for spec in specs:
        raw_targets = spec.get("targets", [])
        targets = [str(item) for item in raw_targets] if isinstance(raw_targets, list) else []
        target_modules = {item.split("#", 1)[0] for item in targets}
        if target not in target_modules:
            continue
        if target_modules != {target}:
            raise JauntConfigError(
                f"Cannot safely eject {target}: test spec {spec.get('path')!r} also targets "
                + ", ".join(sorted(target_modules - {target}))
            )
        spec_path = str(spec.get("path", ""))
        owner = spec.get("project")
        if not isinstance(owner, str):
            owner = _owner_project_for_source(root, config, workspace, spec_path)
        if not isinstance(spec.get("syntheticSource"), str):
            authored = _safe_path(root, spec_path)
            if authored.is_file():
                deletes.add(spec_path)
        for tier in ("example", "derived"):
            generated = _test_output(spec_path, generated_dir, tier)
            generated_path = _safe_path(root, generated)
            if not generated_path.is_file():
                raise JauntConfigError(
                    f"Cannot safely eject {target}: generated {tier} battery is missing: "
                    f"{generated}"
                )
            generated_source = generated_path.read_text(encoding="utf-8")
            if not generated_source.startswith(
                "// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`."
            ):
                raise JauntConfigError(
                    f"Cannot safely eject {target}: refusing to replace unowned test {generated}"
                )
            ordinary_path = _ordinary_test_path(spec_path, tier)
            ordinary = _strip_test_header(generated_source).rstrip() + "\n"
            ordinary = _retarget_relative_imports(ordinary, generated, ordinary_path)
            validation = _static_test_validation(ordinary)
            if validation:
                raise JauntConfigError(
                    f"Cannot safely eject {target}: {ordinary_path}: " + "; ".join(validation)
                )
            _validate_ordinary_ejected_text(ordinary, label=ordinary_path)
            destination = _safe_path(root, ordinary_path)
            if destination.exists() and ordinary_path != generated:
                raise JauntConfigError(
                    f"Cannot safely eject {target}: ordinary test path already exists: "
                    f"{ordinary_path}"
                )
            writes[ordinary_path] = ordinary
            deletes.add(generated)
            owners[ordinary_path] = owner
            owners[generated] = owner
    return writes, deletes, owners


def _module_path_for_eject(module: Mapping[str, Any], key: str) -> str:
    value = module.get(key)
    routes = module.get("routes")
    if not isinstance(value, str) and isinstance(routes, Mapping):
        value = routes.get(key)
    if not isinstance(value, str):
        raise JauntConfigError(f"The worker omitted {key} required for magic ejection")
    return value


def _magic_eject_status_reason(
    status: TargetStatus,
    target: str,
    blocking_status: Sequence[TargetDiagnostic],
) -> str:
    if blocking_status:
        return "; ".join(
            f"{diagnostic.code}: {diagnostic.message}" for diagnostic in blocking_status
        )
    stale = status.stale.get(target)
    if stale is not None:
        return stale
    if target in status.unbuilt:
        return "unbuilt"
    invalid = status.invalid.get(target, ())
    if invalid:
        return "; ".join(f"{diagnostic.code}: {diagnostic.message}" for diagnostic in invalid)
    return "not present in the analyzed workspace"


async def _eject_magic_module(
    root: Path,
    config: JauntConfig,
    target: str,
    worker_factory: WorkerFactory | None,
) -> LifecycleReport:
    from jaunt.typescript.status import run_status

    status = await run_status(root, config, target_ids=(target,), worker_factory=worker_factory)
    blocking_status = tuple(
        diagnostic for diagnostic in status.diagnostics if diagnostic.severity == "error"
    )
    if target not in status.fresh or blocking_status:
        reason = _magic_eject_status_reason(status, target, blocking_status)
        raise JauntConfigError(f"Magic ejection requires a fresh module; {target} is {reason}")
    async with worker_session(root, config, worker_factory=worker_factory) as (client, initialized):
        analysis = await analyze(client, initialized, target_ids=(target,))
        modules = [module for module in analysis.modules if str(module.get("moduleId")) == target]
        if len(modules) != 1:
            raise JauntConfigError(f"Could not resolve one TypeScript module for {target}")
        module = modules[0]
        facade_rel = _module_path_for_eject(module, "facadePath")
        implementation_rel = _module_path_for_eject(module, "implementationPath")
        spec_rel = _module_path_for_eject(module, "specPath")
        api_rel = _module_path_for_eject(module, "apiMirrorPath")
        sidecar_rel = _module_path_for_eject(module, "sidecarPath")
        implementation = _safe_path(root, implementation_rel).read_text(encoding="utf-8")
        ordinary = _ordinary_ejected_source(module, implementation)
        modules_by_id = {str(item.get("moduleId")): item for item in modules}
        test_writes, test_deletes, test_eject_owners = _magic_test_ejection_plan(
            root,
            config,
            analysis.workspace,
            modules_by_id,
            target,
        )
        rewrites: dict[str, str] = {}
        excluded = {spec_rel, implementation_rel, api_rel, facade_rel, *test_deletes}
        for path in _configured_ts_files(root, config):
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            source = path.read_text(encoding="utf-8")
            rewritten = _rewrite_import_target(
                source,
                importer_path=relative,
                old_target_path=api_rel,
                new_target_path=facade_rel,
            )
            rewritten = _rewrite_import_target(
                rewritten,
                importer_path=relative,
                old_target_path=implementation_rel,
                new_target_path=facade_rel,
            )
            if rewritten != source:
                rewrites[relative] = rewritten
        files = _generated_test_files(root, config)
        file_owners = dict(_workspace_test_file_owners(root, config, analysis.workspace))
        file_owners.update(test_eject_owners)
        neutralized = {
            path: "export {};\n"
            for path in test_deletes
            if path.endswith((".test.ts", ".test.tsx"))
        }
        overlays = {facade_rel: ordinary, **rewrites, **test_writes, **neutralized}
        typecheck_files = tuple(sorted(set(files) | set(test_writes)))
        runtime_files = tuple(sorted((set(files) - set(test_deletes)) | set(test_writes)))
        checked = await _run_test_batches(
            client,
            root,
            config,
            analysis.workspace,
            files=typecheck_files,
            explicit_owners=file_owners,
            overlays=overlays,
            typecheck_only=True,
        )
        if not bool(checked.get("ok", False)):
            return LifecycleReport(
                command="eject",
                targets=(target,),
                diagnostics=(
                    TargetDiagnostic(
                        code="JAUNT_TS_EJECT_TYPECHECK",
                        message="The ordinary ejected TypeScript module did not typecheck.",
                    ),
                ),
                exit_code=4,
            )
        project = str(module.get("project", ""))
        from jaunt.typescript import tester as ts_tester

        emitted = await ts_tester._run_test_runner(
            client,
            root,
            config,
            files=(facade_rel,),
            overlays=overlays,
            redact_derived=True,
            typecheck_only=True,
            declaration_emit=True,
            normal_emit=True,
            deleted_files=(spec_rel, implementation_rel, api_rel),
            package_root=str(module.get("packageOwner", ".")),
            tsconfig_path=project or None,
            project_config_paths=_workspace_project_config_paths(analysis.workspace),
        )
        if not bool(emitted.get("ok", False)):
            return LifecycleReport(
                command="eject",
                targets=(target,),
                diagnostics=(
                    TargetDiagnostic(
                        code="JAUNT_TS_EJECT_EMIT",
                        message=(
                            "The ordinary ejected TypeScript module failed protected JavaScript "
                            "and declaration emit validation."
                        ),
                        data={"runner": dict(emitted)},
                    ),
                ),
                exit_code=4,
            )
        if runtime_files:
            run = await _run_test_batches(
                client,
                root,
                config,
                analysis.workspace,
                files=runtime_files,
                explicit_owners=file_owners,
                overlays={facade_rel: ordinary, **rewrites, **test_writes},
            )
            if not bool(run.get("ok", False)):
                return LifecycleReport(
                    command="eject",
                    targets=(target,),
                    diagnostics=(
                        TargetDiagnostic(
                            code="JAUNT_TS_EJECT_TEST_FAILED",
                            message="Tests failed against the ordinary ejected TypeScript module.",
                        ),
                    ),
                    exit_code=4,
                )
    deleted = (spec_rel, implementation_rel, api_rel, sidecar_rel, *tuple(sorted(test_deletes)))
    changed = (facade_rel, *tuple(sorted(rewrites)), *tuple(sorted(test_writes)))
    inputs: dict[str, str] = {}
    for path in {*changed, *deleted}:
        candidate = _safe_path(root, path)
        inputs[path] = _sha256(candidate.read_bytes()) if candidate.exists() else MISSING_INPUT
    atomic_write_manifest(
        root,
        (
            _Write(path=facade_rel, content=ordinary, kind="eject", module_id=target),
            *(
                _Write(path=path, content=source, kind="eject-retarget", module_id=target)
                for path, source in rewrites.items()
            ),
            *(
                _Write(path=path, content=source, kind="eject-test", module_id=target)
                for path, source in test_writes.items()
            ),
            *(
                _Write(path=path, content=None, kind="eject-delete", module_id=target)
                for path in deleted
            ),
        ),
        expected_inputs=inputs,
    )
    append_events(root, [JournalEvent("eject", target, "ordinary TypeScript module")])
    return LifecycleReport(
        command="eject",
        targets=(target,),
        changed=changed,
        removed=deleted,
    )


async def run_eject(
    root: Path,
    config: JauntConfig,
    *,
    target: str,
    worker_factory: WorkerFactory | None = None,
) -> LifecycleReport:
    """Remove contract tracking while leaving ordinary TypeScript and Vitest tests."""

    root = root.resolve()
    if target.startswith("ts:") and "#" not in target:
        return await _eject_magic_module(root, config, target, worker_factory)
    source, symbol = _split_target(root, target)
    original = _read_exact(source)
    target_config = config.typescript_target
    assert target_config is not None
    battery_root = _safe_path(root, target_config.contract_battery_dir)
    source_relative = source.relative_to(root).as_posix()
    async with worker_session(root, config, worker_factory=worker_factory) as (
        client,
        initialized,
    ):
        source_projection = await _project_contract(client, root, source, symbol, original)
        updated = _remove_contract_tag(original, symbol, source_projection)
        proposed: dict[str, str] = {source_relative: updated}
        if battery_root.is_dir():
            for battery in battery_root.rglob("*.contract.test.ts"):
                text = battery.read_text(encoding="utf-8")
                metadata = _battery_header_metadata(text)
                if (
                    metadata is not None
                    and metadata.get("source") == source_relative
                    and f".{symbol}.contract.test.ts" in battery.name
                ):
                    proposed[battery.relative_to(root).as_posix()] = _strip_battery_header(text)
        battery_files = tuple(path for path in proposed if path.endswith((".test.ts", ".test.tsx")))
        analysis = await analyze(client, initialized)
        owner = _owner_project_for_source(root, config, analysis.workspace, source_relative)
        battery_owners = {path: owner for path in battery_files}
        typed = await _run_test_batches(
            client,
            root,
            config,
            analysis.workspace,
            files=battery_files,
            explicit_owners=battery_owners,
            overlays=proposed,
            typecheck_only=True,
        )
        if not bool(typed.get("ok", False)):
            return LifecycleReport(
                command="eject",
                targets=(target,),
                diagnostics=(
                    TargetDiagnostic(
                        code="JAUNT_TS_EJECT_TYPECHECK",
                        message="The ordinary TypeScript contract did not typecheck.",
                    ),
                ),
                exit_code=4,
            )
        if battery_files:
            run = await _run_test_batches(
                client,
                root,
                config,
                analysis.workspace,
                files=battery_files,
                explicit_owners=battery_owners,
                overlays=proposed,
            )
            if not bool(run.get("ok", False)):
                return LifecycleReport(
                    command="eject",
                    targets=(target,),
                    diagnostics=(
                        TargetDiagnostic(
                            code="JAUNT_TS_EJECT_TEST_FAILED",
                            message="The ordinary TypeScript contract battery did not pass.",
                        ),
                    ),
                    exit_code=4,
                )
    atomic_write_manifest(
        root,
        tuple(
            _Write(path=path, content=content, kind="eject", module_id=target)
            for path, content in proposed.items()
        ),
        expected_inputs={
            path: (
                _sha256(original.encode("utf-8"))
                if path == source.relative_to(root).as_posix()
                else _precondition(_safe_path(root, path))
            )
            for path in proposed
        },
    )
    append_events(root, [JournalEvent("eject", target, "left ordinary TypeScript and Vitest")])
    return LifecycleReport(
        command="eject",
        targets=(target,),
        changed=tuple(proposed),
    )


__all__ = ["LifecycleReport", "run_adopt", "run_eject", "run_reconcile"]
