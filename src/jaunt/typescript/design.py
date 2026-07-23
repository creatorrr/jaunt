"""Reviewable declaration-patch flow for ``@jauntDesign`` TypeScript specs."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jaunt.config import JauntConfig
from jaunt.cost import CostTracker
from jaunt.errors import JauntConfigError, JauntGenerationError
from jaunt.generate.base import GenerationRequest, GeneratorBackend, TokenUsage
from jaunt.journal import JournalEvent, append_events
from jaunt.targets.base import TargetDiagnostic
from jaunt.typescript.artifacts import _atomic_text, _fsync_directory
from jaunt.typescript.builder import (
    WorkerFactory,
    _acquire_transaction_lease,
    _default_backend,
    _module_id,
    _module_path,
    _PinnedWorkspace,
    _progress_advance,
    _progress_finish,
    _progress_phase,
    _prompt_text,
    _retire_transaction_manifest,
    _safe_path,
    _sha256,
    _write_transaction_manifest,
    _Write,
    analyze,
    atomic_write_manifest,
    validate_overlay,
    worker_session,
)

_DESIGN_TAG = "@jauntDesign"
_DESIGN_TAG_PATTERN = re.compile(r"(?<![A-Za-z0-9_$])@jauntDesign(?![A-Za-z0-9_$])")
_DESIGN_DECLARATION_PATTERN = re.compile(
    r"\s*export\s+(?:declare\s+)?(?:async\s+)?"
    r"(?P<kind>function|class)\s+(?P<name>[A-Za-z_$][\w$]*)"
)
_DESIGN_PROPOSAL_SCHEMA = "jaunt-ts-design-proposal/1"
_TYPE_IMPORT_FROM_PATTERN = re.compile(
    r"^import\s+type\s+.+?\s+from\s+(['\"])[^'\"\r\n]+\1\s*;$",
    re.DOTALL,
)
_INLINE_TYPE_IMPORT_PATTERN = re.compile(
    r"^import\s*\{(?P<bindings>.*?)\}\s*from\s+(['\"])[^'\"\r\n]+\2\s*;$",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class DesignReport:
    target_id: str
    patch: str = ""
    applied: bool = False
    diagnostics: tuple[TargetDiagnostic, ...] = ()
    usage: Mapping[str, object] | None = None
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class _DesignRange:
    start: int
    end: int
    source: str
    name: str


def _strip_fence(source: str) -> str:
    text = source.strip()
    match = re.fullmatch(r"```(?:typescript|ts)?\s*\n(?P<body>.*)\n```", text, re.DOTALL)
    return match.group("body").strip() if match else text


def _skip_quoted(source: str, start: int, delimiter: str) -> int:
    index = start + 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
            continue
        if source[index] == delimiter:
            return index + 1
        index += 1
    raise JauntConfigError("@jauntDesign source contains an unterminated string")


def _skip_comment(source: str, start: int) -> int:
    if source.startswith("//", start):
        newline = source.find("\n", start + 2)
        return len(source) if newline < 0 else newline + 1
    if source.startswith("/*", start):
        end = source.find("*/", start + 2)
        if end < 0:
            raise JauntConfigError("@jauntDesign source contains an unterminated block comment")
        return end + 2
    return start


def _tsdoc_blocks(source: str) -> tuple[tuple[int, int], ...]:
    """Return lexical TSDoc blocks without crossing strings or comment boundaries."""

    blocks: list[tuple[int, int]] = []
    index = 0
    while index < len(source):
        if source[index] in {'"', "'", "`"}:
            index = _skip_quoted(source, index, source[index])
            continue
        if source.startswith("//", index):
            index = _skip_comment(source, index)
            continue
        if source.startswith("/*", index):
            end = _skip_comment(source, index)
            if source.startswith("/**", index):
                blocks.append((index, end))
            index = end
            continue
        index += 1

    tag_offsets = [match.start() for match in _DESIGN_TAG_PATTERN.finditer(source)]
    for offset in tag_offsets:
        owners = [block for block in blocks if block[0] <= offset < block[1]]
        if len(owners) != 1:
            raise JauntConfigError("@jauntDesign must appear in one complete TSDoc block")
    return tuple(blocks)


def _balanced_brace_end(source: str, start: int) -> int:
    depth = 0
    index = start
    while index < len(source):
        if source[index] in {'"', "'", "`"}:
            index = _skip_quoted(source, index, source[index])
            continue
        if source.startswith(("//", "/*"), index):
            index = _skip_comment(source, index)
            continue
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                break
        index += 1
    raise JauntConfigError("@jauntDesign declaration has unbalanced braces")


def _balanced_declaration_end(source: str, start: int, *, kind: str) -> int:
    stack: list[str] = []
    closing = {")": "(", "]": "[", ">": "<", "}": "{"}
    index = start
    while index < len(source):
        char = source[index]
        if char in {'"', "'", "`"}:
            index = _skip_quoted(source, index, char)
            continue
        if source.startswith(("//", "/*"), index):
            index = _skip_comment(source, index)
            continue
        if char in "([<":
            stack.append(char)
        elif char == "{":
            if not stack:
                end = _balanced_brace_end(source, index)
                if kind == "class":
                    return end
                remainder = re.match(r"\s*;", source[end:])
                if remainder is None:
                    raise JauntConfigError(
                        "@jauntDesign function must be a declaration ending in a semicolon"
                    )
                return end + remainder.end()
            stack.append(char)
        elif char in closing:
            # An arrow's `>` is not a generic delimiter.
            if char == ">" and index > 0 and source[index - 1] == "=":
                index += 1
                continue
            if not stack or stack[-1] != closing[char]:
                raise JauntConfigError("@jauntDesign declaration has unbalanced delimiters")
            stack.pop()
        elif char == ";" and not stack:
            return index + 1
        index += 1
    raise JauntConfigError("@jauntDesign declaration has no bounded declaration end")


def _design_ranges(source: str) -> tuple[_DesignRange, ...]:
    results: list[_DesignRange] = []
    for docs_start, docs_end in _tsdoc_blocks(source):
        docs = source[docs_start:docs_end]
        tags = tuple(_DESIGN_TAG_PATTERN.finditer(docs))
        if not tags:
            continue
        if len(tags) != 1:
            raise JauntConfigError("A TSDoc block may contain only one @jauntDesign tag")
        declaration = _DESIGN_DECLARATION_PATTERN.match(source[docs_end:])
        if declaration is None:
            raise JauntConfigError(
                "@jauntDesign must immediately precede an exported function or class declaration"
            )
        declaration_start = docs_end + declaration.start()
        end = _balanced_declaration_end(
            source,
            declaration_start,
            kind=declaration.group("kind"),
        )
        results.append(
            _DesignRange(
                start=docs_start,
                end=end,
                source=source[docs_start:end],
                name=declaration.group("name"),
            )
        )
    return tuple(results)


def _validate_declaration(source: str) -> list[str]:
    return _design_validation_errors(source)


def _import_statement_end(source: str, start: int) -> int:
    """Find one top-level import terminator without trusting regex across strings."""

    index = start
    brace_depth = 0
    while index < len(source):
        char = source[index]
        if char in {'"', "'", "`"}:
            index = _skip_quoted(source, index, char)
            continue
        if source.startswith(("//", "/*"), index):
            index = _skip_comment(source, index)
            continue
        if char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth -= 1
            if brace_depth < 0:
                raise JauntConfigError("Designed type import has unbalanced braces")
        elif char == ";" and brace_depth == 0:
            return index + 1
        index += 1
    raise JauntConfigError("Designed type import must end in a semicolon")


def _without_comments(source: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(source):
        if source[index] in {'"', "'", "`"}:
            end = _skip_quoted(source, index, source[index])
            output.append(source[index:end])
            index = end
            continue
        if source.startswith(("//", "/*"), index):
            end = _skip_comment(source, index)
            output.append(" " * (end - index))
            index = end
            continue
        output.append(source[index])
        index += 1
    return "".join(output)


def _is_associated_type_import(source: str) -> bool:
    normalized = _without_comments(source).strip()
    if _TYPE_IMPORT_FROM_PATTERN.fullmatch(normalized):
        return True
    inline = _INLINE_TYPE_IMPORT_PATTERN.fullmatch(normalized)
    if inline is None:
        return False
    bindings = [binding.strip() for binding in inline.group("bindings").split(",")]
    return bool(bindings) and all(binding.startswith("type ") for binding in bindings)


def _split_associated_type_imports(source: str) -> tuple[str, list[str]]:
    """Separate the only source outside the designed declaration that may change."""

    index = 0
    errors: list[str] = []
    while True:
        while index < len(source) and source[index].isspace():
            index += 1
        match = re.match(r"import\b", source[index:])
        if match is None:
            break
        end = _import_statement_end(source, index)
        if not _is_associated_type_import(source[index:end]):
            errors.append("associated imports must be type-only imports")
        index = end
    remainder = source[index:]
    if re.search(r"(?m)^\s*import\b", _without_comments(remainder)):
        errors.append("associated type imports must precede the designed declaration")
    return remainder, errors


def _leading_trivia_end(source: str) -> int:
    """Return the first byte that is neither whitespace nor a comment."""

    index = 0
    while index < len(source):
        if source[index].isspace():
            index += 1
            continue
        if source.startswith(("//", "/*"), index):
            index = _skip_comment(source, index)
            continue
        break
    return index


def _confined_design_declaration(source: str) -> tuple[str, str | None, list[str]]:
    """Extract the sole declaration after the optional associated imports.

    Design output is a replacement for one bounded source range, not an
    opportunity to add helper declarations or executable module statements.
    Keeping this check lexical also makes it run before any proposal bytes are
    persisted or exposed to the TypeScript worker.
    """

    remainder, errors = _split_associated_type_imports(source)
    start = _leading_trivia_end(remainder)
    declaration = _DESIGN_DECLARATION_PATTERN.match(remainder[start:])
    if declaration is None:
        errors.append("the replacement must be one exported function or class declaration")
        return "", None, errors

    declaration_start = start + declaration.start()
    try:
        declaration_end = _balanced_declaration_end(
            remainder,
            declaration_start,
            kind=declaration.group("kind"),
        )
    except JauntConfigError as error:
        errors.append(str(error))
        return remainder[declaration_start:], declaration.group("name"), errors

    if remainder[declaration_end:].strip():
        errors.append(
            "the replacement may contain only associated type imports and exactly one "
            "exported function or class declaration"
        )
    return (
        remainder[declaration_start:declaration_end],
        declaration.group("name"),
        errors,
    )


def _design_validation_errors(source: str, *, expected_name: str | None = None) -> list[str]:
    text = _strip_fence(source)
    declaration_text, actual_name, errors = _confined_design_declaration(text)
    if _DESIGN_TAG in text:
        errors.append("the replacement must remove @jauntDesign")
    if declaration_text:
        syntax = _without_comments(declaration_text)
        for pattern, message in (
            (r"\b(?:private|protected)\b", "private/protected members are unsupported"),
            (r"\bany\b", "the declaration may not use any"),
            (
                r"@ts-(?:ignore|expect-error|nocheck)",
                "the declaration may not suppress diagnostics",
            ),
            (r"\breturn\b", "the declaration may not contain executable statements"),
            (r"\bjaunt\.magic\s*\(", "the designed declaration must not contain magic bodies"),
        ):
            if re.search(pattern, syntax):
                errors.append(message)
        # Function designs are declaration-only. Class members may use semicolon
        # forms but not method bodies; an outer class brace pair is the only
        # allowed body.
        if re.search(r"\)\s*\{", syntax):
            errors.append("designed methods must be declarations, not executable bodies")
    if expected_name is not None and actual_name != expected_name:
        errors.append("the replacement must contain exactly the selected exported declaration")
    return errors


def _design_output_errors(source: str, *, expected_name: str) -> list[str]:
    return _design_validation_errors(source, expected_name=expected_name)


def _materialize_magic_stubs(source: str) -> str:
    """Turn a reviewed declaration proposal into the normative private-spec grammar."""

    text = _strip_fence(source).rstrip()
    if re.search(r"\bexport\s+(?:declare\s+)?(?:async\s+)?function\b", text):
        signatures = list(
            re.finditer(
                r"(?m)^\s*export\s+(?:declare\s+)?(?:async\s+)?function\s+"
                r"[A-Za-z_$][\w$]*(?:<[^;{}]*>)?\([^;{}]*\)\s*(?::\s*[^;{}]+)?\s*;",
                text,
            )
        )
        if not signatures:
            raise JauntConfigError("Designed function must end in a declaration signature")
        implementation = signatures[-1]
        signature = re.sub(r"\bdeclare\s+", "", implementation.group(0)).rstrip()[:-1]
        return (
            text[: implementation.start()]
            + signature
            + " { return jaunt.magic(); }"
            + text[implementation.end() :]
        )

    member_pattern = re.compile(
        r"(?m)(?:^|(?<=[;{}]))(?P<indent>\s*)"
        r"(?P<prefix>(?:(?:public|static|async|readonly|abstract|override)\s+)*)"
        r"(?P<accessor>(?:get|set)\s+)?(?P<name>constructor|[A-Za-z_$][\w$]*)"
        r"(?P<tail>\s*(?:<[^;{}]*>)?\([^;{}]*\)\s*(?::\s*[^;{}]+)?\s*);"
    )
    matches = list(member_pattern.finditer(text))
    last_by_group: dict[tuple[str, str], re.Match[str]] = {}
    for match in matches:
        static = "static" if "static" in match.group("prefix").split() else "instance"
        last_by_group[(static, match.group("name"))] = match
    for match in sorted(last_by_group.values(), key=lambda item: item.start(), reverse=True):
        signature = match.group(0).rstrip()[:-1]
        bare = (
            match.group("name") == "constructor"
            or (match.group("accessor") or "").strip() == "set"
            or re.search(r":\s*void\s*$", match.group("tail")) is not None
        )
        body = " { jaunt.magic(); }" if bare else " { return jaunt.magic(); }"
        text = text[: match.start()] + signature + body + text[match.end() :]
    return re.sub(r"\bexport\s+declare\s+class\b", "export class", text)


def _design_patch(*, current: str, updated: str, relative_spec: str) -> str:
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=relative_spec,
            tofile=relative_spec,
        )
    )


def _proposal_path(root: Path, *, target_id: str, relative_spec: str) -> Path:
    identity = json.dumps(
        {"targetId": target_id, "sourcePath": relative_spec},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return root / ".jaunt" / "design-proposals" / f"{digest}.json"


def _write_design_proposal(
    path: Path,
    *,
    target_id: str,
    relative_spec: str,
    source_digest: str,
    design: _DesignRange,
    declaration: str,
    replacement: str,
    updated: str,
    patch: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": _DESIGN_PROPOSAL_SCHEMA,
        "targetId": target_id,
        "sourcePath": relative_spec,
        "sourceDigest": source_digest,
        "rangeStart": design.start,
        "rangeEnd": design.end,
        "rangeDigest": _sha256(design.source.encode("utf-8")),
        "declaration": declaration,
        "replacement": replacement,
        "replacementDigest": _sha256(replacement.encode("utf-8")),
        "updatedDigest": _sha256(updated.encode("utf-8")),
        "patch": patch,
    }
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _load_design_proposal(
    path: Path,
    *,
    current: str,
    source_digest: str,
    target_id: str,
    relative_spec: str,
    design: _DesignRange,
) -> tuple[str, str, str] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise JauntGenerationError(
            "The stored design proposal is unreadable; run `jaunt design` again without --apply"
        ) from error
    expected_keys = {
        "schema",
        "targetId",
        "sourcePath",
        "sourceDigest",
        "rangeStart",
        "rangeEnd",
        "rangeDigest",
        "declaration",
        "replacement",
        "replacementDigest",
        "updatedDigest",
        "patch",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise JauntGenerationError(
            "The stored design proposal has an invalid shape; "
            "run `jaunt design` again without --apply"
        )
    for key in (
        "schema",
        "targetId",
        "sourcePath",
        "sourceDigest",
        "rangeDigest",
        "declaration",
        "replacement",
        "replacementDigest",
        "updatedDigest",
        "patch",
    ):
        if not isinstance(payload[key], str):
            raise JauntGenerationError(
                "The stored design proposal has invalid fields; "
                "run `jaunt design` again without --apply"
            )
    if type(payload["rangeStart"]) is not int or type(payload["rangeEnd"]) is not int:
        raise JauntGenerationError(
            "The stored design proposal has invalid ranges; "
            "run `jaunt design` again without --apply"
        )
    if payload["sourceDigest"] != source_digest:
        raise JauntGenerationError(
            "The design source changed since the reviewed proposal; "
            "run `jaunt design` again without --apply"
        )
    identity = (
        payload["schema"] == _DESIGN_PROPOSAL_SCHEMA
        and payload["targetId"] == target_id
        and payload["sourcePath"] == relative_spec
        and payload["rangeStart"] == design.start
        and payload["rangeEnd"] == design.end
        and payload["rangeDigest"] == _sha256(design.source.encode("utf-8"))
    )
    if not identity:
        raise JauntGenerationError(
            "The stored design proposal no longer matches the selected declaration; "
            "run `jaunt design` again without --apply"
        )
    declaration = payload["declaration"]
    errors = _design_output_errors(declaration, expected_name=design.name)
    if errors:
        raise JauntGenerationError(
            "The stored design proposal is invalid: "
            + "; ".join(errors)
            + "; run `jaunt design` again without --apply"
        )
    replacement = _materialize_magic_stubs(declaration)
    if "\r\n" in current:
        replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
    updated = current[: design.start] + replacement + current[design.end :]
    patch = _design_patch(current=current, updated=updated, relative_spec=relative_spec)
    integrity = (
        payload["replacement"] == replacement
        and payload["replacementDigest"] == _sha256(replacement.encode("utf-8"))
        and payload["updatedDigest"] == _sha256(updated.encode("utf-8"))
        and payload["patch"] == patch
    )
    if not integrity:
        raise JauntGenerationError(
            "The stored design proposal failed its integrity check; "
            "run `jaunt design` again without --apply"
        )
    return replacement, updated, patch


def _discard_design_proposal(path: Path) -> None:
    if not path.exists():
        return
    path.unlink()
    _fsync_directory(path.parent)


def _git_dirty(root: Path, path: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", relative],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _prepare_design_manifest(
    root: Path,
    *,
    path: str,
    module_id: str,
    before: str,
    after: str,
) -> Path:
    """Durably mark the validation window before exposing proposed source bytes."""

    root = root.resolve()
    directory = root / ".jaunt" / "transactions"
    manifest = directory / f"design-{uuid.uuid4().hex}.json"
    payload = {
        "state": "prepared",
        "operation": "design",
        "writes": [
            {
                "path": path,
                "kind": "design",
                "moduleId": module_id,
                "before": _sha256(before.encode("utf-8")),
                "after": _sha256(after.encode("utf-8")),
            }
        ],
    }
    with _PinnedWorkspace(root) as workspace:
        pinned_directory = workspace.directory(directory)
        lease = _acquire_transaction_lease(
            directory,
            blocking=True,
            pinned_directory=pinned_directory,
            authority_directory=workspace.root_directory,
        )
        if lease is None:  # pragma: no cover - blocking acquisition
            raise JauntGenerationError("Could not acquire TypeScript transaction lease")
        try:
            pending_manifests = pinned_directory.iter_names("*.json")
            if pending_manifests:
                raise JauntGenerationError(
                    "An unresolved TypeScript artifact transaction blocks design publication: "
                    + ", ".join(pending_manifests)
                )
            workspace.verify_namespace()
            _write_transaction_manifest(
                manifest,
                payload,
                pinned_directory=pinned_directory,
            )
            workspace.verify_namespace()
        finally:
            lease.release()
    return manifest


def _complete_design_manifest(root: Path, manifest: Path) -> None:
    """Durably retire exactly one owned design marker under the global lease."""

    root = root.resolve()
    directory = root / ".jaunt" / "transactions"
    absolute_manifest = Path(os.path.abspath(manifest))
    if absolute_manifest.parent != directory:
        raise JauntGenerationError("Design transaction marker is outside the workspace journal")
    manifest = absolute_manifest
    with _PinnedWorkspace(root) as workspace:
        try:
            pinned_directory = workspace.directory(directory, create=False)
        except FileNotFoundError as error:
            raise JauntGenerationError("Design transaction marker directory is missing") from error
        lease = _acquire_transaction_lease(
            directory,
            blocking=True,
            pinned_directory=pinned_directory,
            authority_directory=workspace.root_directory,
        )
        if lease is None:  # pragma: no cover - blocking acquisition
            raise JauntGenerationError("Could not acquire TypeScript transaction lease")
        try:
            manifests = pinned_directory.iter_names("*.json")
            foreign_manifests = tuple(name for name in manifests if name != manifest.name)
            if foreign_manifests:
                raise JauntGenerationError(
                    "An unresolved TypeScript artifact transaction blocks design completion: "
                    + ", ".join(foreign_manifests)
                )
            if manifest.name not in manifests:
                raise JauntGenerationError(f"Design transaction marker is missing: {manifest.name}")
            try:
                payload = json.loads(pinned_directory.read_bytes(manifest.name).decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise JauntGenerationError(
                    f"Design transaction marker is invalid: {manifest.name}"
                ) from error
            if not isinstance(payload, Mapping) or not (
                payload.get("state") == "prepared" and payload.get("operation") == "design"
            ):
                raise JauntGenerationError(f"Design transaction marker is invalid: {manifest.name}")
            workspace.verify_namespace()
            if not _retire_transaction_manifest(
                manifest,
                payload,
                pinned_directory=pinned_directory,
            ):
                raise JauntGenerationError(
                    "Design transaction completed, but its recovery marker could not be "
                    "durably retired"
                )
            workspace.verify_namespace()
        finally:
            lease.release()


async def run_design(
    root: Path,
    config: JauntConfig,
    *,
    target_id: str | None = None,
    apply: bool = False,
    force: bool = False,
    generator: GeneratorBackend | None = None,
    cost_tracker: CostTracker | None = None,
    progress: object | None = None,
    worker_factory: WorkerFactory | None = None,
    max_attempts: int = 2,
    auto_skills_enabled: bool | None = None,
    builtin_skill_names: Sequence[str] | None = None,
) -> DesignReport:
    """Propose, and optionally apply, one declaration-only design patch."""

    root = root.resolve()
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    from jaunt.typescript.builder import _target

    target_config = _target(config)
    use_auto_skills = (
        target_config.auto_skills_enabled(bool(config.skills.auto))
        if auto_skills_enabled is None
        else auto_skills_enabled
    )
    if use_auto_skills and not apply:
        from jaunt.skills_npm import ensure_npm_skills, typescript_package_owners

        ensure_npm_skills(
            project_root=root,
            package_owners=typescript_package_owners(root, target_config),
            max_readme_chars=config.skills.max_chars_per_skill,
        )
    async with worker_session(root, config, worker_factory=worker_factory) as (client, initialized):
        analysis = await analyze(
            client,
            initialized,
            target_ids=(target_id,) if target_id and target_id.startswith("ts:") else (),
        )
    candidates: list[tuple[Mapping[str, Any], _DesignRange, str]] = []
    for module in analysis.modules:
        source = str(module.get("specSource", ""))
        for design in _design_ranges(source):
            symbol_id = f"{_module_id(module)}#{design.name}"
            if target_id is None or target_id in {_module_id(module), symbol_id}:
                candidates.append((module, design, symbol_id))
    if not candidates:
        raise JauntConfigError("No matching @jauntDesign declaration was found")
    if len(candidates) != 1:
        names = ", ".join(item[2] for item in candidates)
        raise JauntConfigError(f"Design target is ambiguous; choose one of: {names}")
    module, design, symbol_id = candidates[0]
    spec_path = _safe_path(root, _module_path(module, "specPath"))
    with spec_path.open("r", encoding="utf-8", newline="") as stream:
        current = stream.read()
    analyzed = str(module.get("specSource", ""))
    if current != analyzed:
        raise JauntGenerationError("The design source changed after analyzer discovery")
    source_digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
    relative_spec = spec_path.relative_to(root).as_posix()
    proposal_path = _proposal_path(root, target_id=symbol_id, relative_spec=relative_spec)
    if apply and not force and _git_dirty(root, spec_path):
        raise JauntConfigError(
            f"Refusing to apply a design over uncommitted changes in {relative_spec}"
        )
    cost = cost_tracker or CostTracker(max_cost=config.llm.max_cost_per_build)
    stored = (
        _load_design_proposal(
            proposal_path,
            current=current,
            source_digest=source_digest,
            target_id=symbol_id,
            relative_spec=relative_spec,
            design=design,
        )
        if apply
        else None
    )
    if apply and stored is None:
        raise JauntConfigError(
            "No reviewed design proposal is available; run `jaunt design` without --apply first"
        )
    if stored is not None:
        replacement, updated, patch = stored
    else:
        # A failed re-preview must not leave an older proposal available to apply.
        _discard_design_proposal(proposal_path)
        system = _prompt_text(config.typescript_prompts.design_system, "design_system.md")
        user = _prompt_text(config.typescript_prompts.design_user, "design_user.md")
        request = GenerationRequest(
            language="ts",
            kind="design",
            target_path="designed-declaration.ts",
            context_files={
                "_context/spec.ts": current,
                "_context/design.json": json.dumps(
                    {
                        "targetId": symbol_id,
                        "start": design.start,
                        "end": design.end,
                        "sourceDigest": source_digest,
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            },
            prompt=f"{system}\n\n{user}",
            cache_payload={"targetId": symbol_id, "sourceDigest": source_digest},
            validator=lambda source: _design_output_errors(
                source,
                expected_name=design.name,
            ),
            project_root=root,
            builtin_skill_names=effective_builtin_skills,
        )
        backend = generator or _default_backend(config)
        # Design proposals are deliberately not response-cached: every preview is a
        # newly reviewable proposal, while apply consumes only the exact persisted
        # proposal bytes and never calls the model.
        _progress_phase(progress, symbol_id, "generating design proposal")

        def record_usage(usage: TokenUsage) -> None:
            cost.record(symbol_id, usage)
            cost.check_budget()

        result = await backend.generate_request_with_retry(
            request,
            max_attempts=max_attempts,
            progress=lambda stage, detail: _progress_phase(progress, symbol_id, stage, detail),
            usage_callback=record_usage,
        )
        if result.source is None or result.errors:
            diagnostics = tuple(
                TargetDiagnostic(code="JAUNT_TS_DESIGN", message=error)
                for error in result.errors or ["The model returned no declaration"]
            )
            _progress_advance(progress, symbol_id, ok=False)
            _progress_finish(progress)
            return DesignReport(target_id=symbol_id, diagnostics=diagnostics, exit_code=3)
        declaration = _strip_fence(result.source)
        replacement = _materialize_magic_stubs(declaration)
        if "\r\n" in current:
            replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
        updated = current[: design.start] + replacement + current[design.end :]
        patch = _design_patch(current=current, updated=updated, relative_spec=relative_spec)
        if hashlib.sha256(spec_path.read_bytes()).hexdigest() != source_digest:
            raise JauntGenerationError("The design source changed while generating the proposal")
        _write_design_proposal(
            proposal_path,
            target_id=symbol_id,
            relative_spec=relative_spec,
            source_digest=source_digest,
            design=design,
            declaration=declaration,
            replacement=replacement,
            updated=updated,
            patch=patch,
        )
        _progress_advance(progress, symbol_id, ok=True)
        _progress_finish(progress)
        return DesignReport(target_id=symbol_id, patch=patch, usage=cost.summary_dict())
    if hashlib.sha256(spec_path.read_bytes()).hexdigest() != source_digest:
        raise JauntGenerationError("The design source changed after the proposal was generated")

    module_id = _module_id(module)
    # This outer prepared marker spans both replacement and fresh analysis. If the
    # process dies in that window, status/check see an incomplete transaction
    # instead of accepting the unvalidated proposal as committed source.
    manifest = _prepare_design_manifest(
        root,
        path=relative_spec,
        module_id=module_id,
        before=current,
        after=updated,
    )
    wrote_proposal = False
    try:
        atomic_write_manifest(
            root,
            (_Write(relative_spec, updated, "design", symbol_id),),
            expected_inputs={relative_spec: f"sha256:{source_digest}"},
            allowed_transaction_manifests=(manifest.name,),
        )
        wrote_proposal = True
        async with worker_session(root, config, worker_factory=worker_factory) as (
            client,
            initialized,
        ):
            fresh_analysis = await analyze(client, initialized, target_ids=(module_id,))
            validated = await validate_overlay(
                client,
                fresh_analysis,
                {},
                (module_id,),
                sync_module_ids=(module_id,),
                scoped_validation=True,
            )
            if not validated.valid:
                rendered = "; ".join(
                    f"{diagnostic.code}: {diagnostic.message}"
                    + (f" ({diagnostic.path})" if diagnostic.path else "")
                    for diagnostic in validated.diagnostics
                )
                raise JauntGenerationError(
                    "Designed declaration failed semantic TypeScript validation"
                    + (f": {rendered}" if rendered else "")
                )
    except BaseException:
        try:
            if wrote_proposal:
                atomic_write_manifest(
                    root,
                    (_Write(relative_spec, current, "design-rollback", symbol_id),),
                    expected_inputs={relative_spec: _sha256(updated.encode("utf-8"))},
                    allowed_transaction_manifests=(manifest.name,),
                )
        except BaseException:
            # Preserve the prepared marker when rollback itself cannot complete.
            raise
        else:
            _complete_design_manifest(root, manifest)
        raise
    _complete_design_manifest(root, manifest)
    _discard_design_proposal(proposal_path)
    append_events(root, [JournalEvent("design", symbol_id, "applied declaration patch")])
    _progress_finish(progress)
    return DesignReport(
        target_id=symbol_id,
        patch=patch,
        applied=True,
        usage=cost.summary_dict(),
    )


__all__ = ["DesignReport", "run_design"]
