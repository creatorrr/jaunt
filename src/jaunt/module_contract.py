"""Handwritten source-module contracts for generation context and freshness."""

from __future__ import annotations

import ast
import hashlib
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef, normalize_spec_ref, normalize_spec_refs


@dataclass(frozen=True, slots=True)
class HandwrittenSymbol:
    name: str
    kind: Literal["function", "async_function", "class", "assignment"]
    signature: str = ""
    doc_summary: str = ""
    excerpt: str = ""
    source_segment: str = ""
    ast_dump: str = ""


@dataclass(frozen=True, slots=True)
class ModuleContract:
    source_file: str
    digest: str
    prompt_block: str
    handwritten_names: tuple[str, ...]
    symbols: tuple[HandwrittenSymbol, ...] = ()


def build_module_contract(
    *,
    entries: list[SpecEntry],
    expected_names: list[str],
    generated_names: list[str] | None = None,
) -> ModuleContract:
    if not entries:
        empty_digest = hashlib.sha256(b"[]").hexdigest()
        return ModuleContract(
            source_file="",
            digest=empty_digest,
            prompt_block="(none)\n",
            handwritten_names=(),
            symbols=(),
        )

    source_file = entries[0].source_file
    source = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=source_file)
    generated = set(generated_names or expected_names)

    symbols: list[HandwrittenSymbol] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name not in generated:
            symbols.append(
                HandwrittenSymbol(
                    name=node.name,
                    kind="function",
                    signature=_signature_line(source, node),
                    doc_summary=_first_doc_line(node),
                    source_segment=_clean_source_segment(source, node),
                    ast_dump=ast.dump(node, include_attributes=False),
                )
            )
            continue

        if isinstance(node, ast.AsyncFunctionDef) and node.name not in generated:
            symbols.append(
                HandwrittenSymbol(
                    name=node.name,
                    kind="async_function",
                    signature=_signature_line(source, node),
                    doc_summary=_first_doc_line(node),
                    source_segment=_clean_source_segment(source, node),
                    ast_dump=ast.dump(node, include_attributes=False),
                )
            )
            continue

        if isinstance(node, ast.ClassDef) and node.name not in generated:
            symbols.append(
                HandwrittenSymbol(
                    name=node.name,
                    kind="class",
                    signature=_class_signature(node),
                    doc_summary=_first_doc_line(node),
                    excerpt=_class_notes(node),
                    source_segment=_clean_source_segment(source, node),
                    ast_dump=ast.dump(node, include_attributes=False),
                )
            )
            continue

        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for name in _assignment_names(node):
                if name in generated:
                    continue
                symbols.append(
                    HandwrittenSymbol(
                        name=name,
                        kind="assignment",
                        excerpt=_short_excerpt(ast.unparse(node)),
                        source_segment=_clean_source_segment(source, node),
                        ast_dump=ast.dump(node, include_attributes=False),
                    )
                )

    prompt_block = _format_prompt_block(symbols)
    digest_payload = [
        {
            "name": symbol.name,
            "kind": symbol.kind,
            "signature": symbol.signature,
            "doc_summary": symbol.doc_summary,
            "excerpt": symbol.excerpt,
            "ast_dump": symbol.ast_dump,
        }
        for symbol in symbols
    ]
    raw = json.dumps(digest_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    handwritten_names = tuple(symbol.name for symbol in symbols)
    return ModuleContract(
        source_file=source_file,
        digest=digest,
        prompt_block=prompt_block,
        handwritten_names=handwritten_names,
        symbols=tuple(symbols),
    )


def test_public_api_only_by_name(entries: list[SpecEntry]) -> dict[str, bool]:
    policies: dict[str, bool] = {}
    for entry in entries:
        raw = entry.decorator_kwargs.get("public_api_only")
        if raw is None:
            policies[entry.qualname] = True
        elif isinstance(raw, bool):
            policies[entry.qualname] = raw
        else:
            policies[entry.qualname] = bool(raw)
    return policies


def target_refs_by_test_name(entries: list[SpecEntry]) -> dict[str, tuple[SpecRef, ...]]:
    targets: dict[str, tuple[SpecRef, ...]] = {}
    for entry in entries:
        raw = entry.decorator_kwargs.get("targets")
        if raw is None:
            targets[entry.qualname] = ()
            continue
        targets[entry.qualname] = normalize_spec_refs(raw)
    return targets


def target_modules_by_name(entries: list[SpecEntry]) -> dict[str, tuple[str, ...]]:
    targets: dict[str, tuple[str, ...]] = {}
    for qualname, refs in target_refs_by_test_name(entries).items():
        modules: list[str] = []
        for ref in refs:
            module_name, _, _target_qualname = str(ref).partition(":")
            if module_name and module_name not in modules:
                modules.append(module_name)
        targets[qualname] = tuple(modules)
    return targets


def group_test_entries_by_target_module(entries: list[SpecEntry]) -> dict[str, list[SpecEntry]]:
    grouped: dict[str, list[SpecEntry]] = {}
    for entry in sorted(entries, key=lambda item: (item.module, item.qualname, str(item.spec_ref))):
        seen_modules: set[str] = set()
        for ref in target_refs_by_test_name([entry]).get(entry.qualname, ()):
            module_name, _, _target_qualname = str(ref).partition(":")
            if not module_name or module_name in seen_modules:
                continue
            grouped.setdefault(module_name, []).append(entry)
            seen_modules.add(module_name)
    return grouped


def extract_targeted_test_entries(module_name: str, source_file: str) -> list[SpecEntry]:
    source = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=source_file)
    imported_modules, imported_specs = _collect_target_imports(tree)

    entries: list[SpecEntry] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        targets = _targets_for_test_node(
            node,
            imported_modules=imported_modules,
            imported_specs=imported_specs,
        )
        if targets is None:
            continue
        spec_ref = normalize_spec_ref(f"{module_name}:{node.name}")
        entries.append(
            SpecEntry(
                kind="test",
                spec_ref=spec_ref,
                module=module_name,
                qualname=node.name,
                source_file=source_file,
                obj=object(),
                decorator_kwargs={"targets": targets},
            )
        )
    return entries


def extract_spec_preamble(source_file: str) -> str:
    """Return source text before the first jaunt-decorated definition."""

    source = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=source_file)
    first_lineno: int | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(
            _is_jaunt_decorator(dec) for dec in node.decorator_list
        ):
            first_lineno = node.lineno
            break
    if first_lineno is None or first_lineno <= 1:
        return ""
    lines = source.splitlines()
    preamble = "\n".join(lines[: first_lineno - 1]).rstrip()
    if not preamble:
        return ""
    return preamble + "\n"


def _signature_line(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    seg = ast.get_source_segment(source, node) or ""
    first = next((line.strip() for line in seg.splitlines() if line.strip()), "")
    if first.startswith("@"):
        return f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}(...)"
    if first:
        return first.removesuffix(":")
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}(...)"


def _class_signature(node: ast.ClassDef) -> str:
    bases = ", ".join(ast.unparse(base) for base in node.bases)
    if bases:
        return f"class {node.name}({bases})"
    return f"class {node.name}"


def _class_notes(node: ast.ClassDef) -> str:
    decorators = [ast.unparse(dec) for dec in node.decorator_list]
    if not decorators:
        return ""
    return "decorators: " + ", ".join(decorators)


def _clean_source_segment(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node) or ""
    if not segment:
        return ""
    segment = textwrap.dedent(segment)
    segment = segment.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in segment.splitlines()]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _first_doc_line(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module,
) -> str:
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _assignment_names(node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> list[str]:
    if isinstance(node, ast.Assign):
        names: list[str] = []
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
        return names

    target = node.target
    if isinstance(target, ast.Name):
        return [target.id]
    return []


def _short_excerpt(text: str, *, limit: int = 140) -> str:
    flattened = " ".join((text or "").strip().split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 3].rstrip() + "..."


def _format_prompt_block(symbols: list[HandwrittenSymbol]) -> str:
    if not symbols:
        return "(none)\n"

    chunks: list[str] = []
    for symbol in symbols:
        lines = [f"kind: {symbol.kind}"]
        if symbol.signature:
            lines.append(f"signature: {symbol.signature}")
        if symbol.doc_summary:
            lines.append(f"doc: {symbol.doc_summary}")
        if symbol.excerpt:
            lines.append(f"notes: {symbol.excerpt}")
        if symbol.source_segment:
            lines.append("source:")
            lines.append(_indent(symbol.source_segment))
        chunks.append(f"# {symbol.name}\n" + "\n".join(lines))
    return "\n\n".join(chunks).rstrip() + "\n"


def _indent(text: str, *, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.splitlines())


def _collect_target_imports(tree: ast.Module) -> tuple[dict[str, str], dict[str, SpecRef]]:
    imported_modules: dict[str, str] = {}
    imported_specs: dict[str, SpecRef] = {}

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".", 1)[0]
                imported_modules[bound_name] = alias.name.split(".", 1)[0]
                if alias.asname is not None:
                    imported_modules[bound_name] = alias.name
            continue

        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not module:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            imported_specs[alias.asname or alias.name] = normalize_spec_ref(
                f"{module}:{alias.name}"
            )

    return imported_modules, imported_specs


def _targets_for_test_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    imported_modules: dict[str, str],
    imported_specs: dict[str, SpecRef],
) -> tuple[SpecRef, ...] | None:
    for dec in node.decorator_list:
        if not _is_test_decorator(dec):
            continue
        if not isinstance(dec, ast.Call):
            return None
        for kw in dec.keywords:
            if kw.arg != "targets" or kw.value is None:
                continue
            return _resolve_target_expr(
                kw.value,
                imported_modules=imported_modules,
                imported_specs=imported_specs,
            )
        return None
    return None


def _resolve_target_expr(
    node: ast.expr,
    *,
    imported_modules: dict[str, str],
    imported_specs: dict[str, SpecRef],
) -> tuple[SpecRef, ...]:
    refs = tuple(
        _iter_target_refs(
            node,
            imported_modules=imported_modules,
            imported_specs=imported_specs,
        )
    )
    return normalize_spec_refs(refs)


def _iter_target_refs(
    node: ast.expr,
    *,
    imported_modules: dict[str, str],
    imported_specs: dict[str, SpecRef],
) -> list[SpecRef]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [normalize_spec_ref(node.value)]

    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        refs: list[SpecRef] = []
        for elt in node.elts:
            refs.extend(
                _iter_target_refs(
                    elt,
                    imported_modules=imported_modules,
                    imported_specs=imported_specs,
                )
            )
        return refs

    if isinstance(node, ast.Name):
        resolved = imported_specs.get(node.id)
        if resolved is not None:
            return [resolved]
        raise ValueError(f"Unsupported target reference: {ast.unparse(node)}")

    if isinstance(node, ast.Attribute):
        base, attrs = _flatten_attribute(node)
        if base in imported_specs:
            spec_ref = str(imported_specs[base])
            module_name, _, qualname = spec_ref.partition(":")
            return [SpecRef(f"{module_name}:{qualname}.{'.'.join(attrs)}")]
        if base in imported_modules:
            module_name, qualname = _module_and_qualname_from_import(imported_modules[base], attrs)
            return [SpecRef(f"{module_name}:{qualname}")]
        raise ValueError(f"Unsupported target reference: {ast.unparse(node)}")

    raise ValueError(f"Unsupported target reference: {ast.unparse(node)}")


def _flatten_attribute(node: ast.Attribute) -> tuple[str, list[str]]:
    attrs: list[str] = [node.attr]
    cur = node.value
    while isinstance(cur, ast.Attribute):
        attrs.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        raise ValueError(f"Unsupported target reference: {ast.unparse(node)}")
    attrs.reverse()
    return cur.id, attrs


def _module_and_qualname_from_import(module_name: str, attrs: list[str]) -> tuple[str, str]:
    if not attrs:
        raise ValueError(f"Unsupported target reference: {module_name}")

    split_at = next((i for i, part in enumerate(attrs) if part and part[0].isupper()), None)
    if split_at is None:
        extra_module = attrs[:-1]
        qualname = attrs[-1]
    else:
        extra_module = attrs[:split_at]
        qualname = ".".join(attrs[split_at:])

    resolved_module = module_name
    if extra_module:
        resolved_module += "." + ".".join(extra_module)
    return resolved_module, qualname


def _is_test_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "jaunt"
            and target.attr == "test"
        )
    if isinstance(target, ast.Name):
        return target.id == "test"
    return False


def _is_jaunt_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "jaunt"
            and target.attr in {"magic", "test"}
        )
    if isinstance(target, ast.Name):
        return target.id in {"magic", "test"}
    return False
