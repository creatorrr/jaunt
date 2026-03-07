"""Decorator-aware metadata extraction for magic specs."""

from __future__ import annotations

import ast
import copy
import inspect
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jaunt.registry import DecoratorApiRecord
from jaunt.spec_ref import SpecRef, spec_ref_from_object


@dataclass(frozen=True, slots=True)
class DecoratorAnalysis:
    auto_deps: tuple[SpecRef, ...]
    records: tuple[DecoratorApiRecord, ...]
    effective_signature: str | None
    effective_signature_source: Literal["decorated", "original"] | None
    warnings: tuple[str, ...]


def _find_nested_node(tree: ast.Module, qualname: str) -> ast.AST | None:
    parts = qualname.split(".")
    node: ast.AST = tree
    for part in parts:
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            return None
        found = False
        for child in body:
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and child.name == part
            ):
                node = child
                found = True
                break
        if not found:
            return None
    return node


def _find_node_for_qualname(tree: ast.Module, qualname: str) -> ast.AST | None:
    if "." in qualname:
        return _find_nested_node(tree, qualname)
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == qualname
        ):
            return node
    return None


def _is_magic_decorator(node: ast.AST) -> bool:
    base = node.func if isinstance(node, ast.Call) else node

    if isinstance(base, ast.Name):
        return base.id == "magic"
    if isinstance(base, ast.Attribute):
        return (
            isinstance(base.value, ast.Name) and base.value.id == "jaunt" and base.attr == "magic"
        )
    return False


def _name_chain(node: ast.AST) -> list[str] | None:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        parent = _name_chain(target.value)
        if not parent:
            return None
        return [*parent, target.attr]
    return None


def _symbol_paths_for_decorator(node: ast.AST) -> list[str]:
    chain = _name_chain(node)
    if not chain:
        return []
    out: list[str] = []
    for i in range(1, len(chain) + 1):
        out.append(".".join(chain[:i]))
    return out


def _resolve_path(module_name: str, symbol_path: str) -> object | None:
    mod = sys.modules.get(module_name)
    if mod is None:
        return None

    parts = [p for p in symbol_path.split(".") if p]
    if not parts:
        return None

    if not hasattr(mod, parts[0]):
        return None

    current: object = getattr(mod, parts[0])
    for part in parts[1:]:
        try:
            current = getattr(current, part)
        except Exception:
            return None
    return current


def _target_label(obj: object | None) -> str | None:
    if obj is None:
        return None
    module = getattr(obj, "__module__", None)
    qualname = getattr(obj, "__qualname__", None)
    if isinstance(module, str) and isinstance(qualname, str):
        return f"{module}.{qualname}"
    obj_t = type(obj)
    mod2 = getattr(obj_t, "__module__", None)
    q2 = getattr(obj_t, "__qualname__", None)
    if isinstance(mod2, str) and isinstance(q2, str):
        return f"{mod2}.{q2}"
    return None


def _best_effort_dep_ref(obj: object | None) -> SpecRef | None:
    if obj is None:
        return None
    try:
        return spec_ref_from_object(obj)
    except Exception:
        pass
    try:
        return spec_ref_from_object(type(obj))
    except Exception:
        return None


def _signature_quality(
    sig: inspect.Signature | None,
) -> Literal["good", "weak", "missing", "unknown"]:
    if sig is None:
        return "missing"
    params = list(sig.parameters.values())
    has_named = any(
        p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        for p in params
    )
    annotated_params = any(p.annotation is not inspect.Signature.empty for p in params)
    has_ret = sig.return_annotation is not inspect.Signature.empty
    if has_named and (annotated_params or has_ret):
        return "good"
    return "weak"


def _signature_for_obj(
    obj: object | None,
) -> tuple[str | None, Literal["good", "weak", "missing", "unknown"]]:
    if obj is None:
        return None, "unknown"
    sig_target = obj if callable(obj) else type(obj)
    try:
        sig = inspect.signature(sig_target)
    except Exception:
        return None, "missing"
    return str(sig), _signature_quality(sig)


def _signature_from_function_node(node: ast.AST) -> str | None:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None

    # Keep this best-effort and representation-stable by asking AST unparse
    # for a normalized function header.
    clone = copy.deepcopy(node)
    clone.decorator_list = []
    clone.body = [ast.Pass()]
    try:
        rendered = ast.unparse(clone)
    except Exception:
        return None

    marker = (
        f"async def {node.name}" if isinstance(node, ast.AsyncFunctionDef) else f"def {node.name}"
    )
    i = rendered.find(marker)
    if i < 0:
        return None
    snippet = rendered[i:]
    header_lines: list[str] = []
    for line in snippet.splitlines():
        header_lines.append(line.strip())
        if line.rstrip().endswith(":"):
            break
    if not header_lines:
        return None
    header = " ".join(header_lines).rsplit(":", 1)[0]
    k = header.find("(")
    if k < 0:
        return None
    return header[k:]


def analyze_magic_decorators(
    *,
    module: str,
    qualname: str,
    source_file: str,
    decorated_obj: object,
) -> DecoratorAnalysis:
    try:
        source = Path(source_file).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=source_file)
    except Exception:
        sig_text, _quality = _signature_for_obj(decorated_obj)
        return DecoratorAnalysis(
            auto_deps=(),
            records=(),
            effective_signature=sig_text,
            effective_signature_source="decorated" if sig_text is not None else None,
            warnings=(),
        )

    node = _find_node_for_qualname(tree, qualname)
    if node is None:
        sig_text, _quality = _signature_for_obj(decorated_obj)
        return DecoratorAnalysis(
            auto_deps=(),
            records=(),
            effective_signature=sig_text,
            effective_signature_source="decorated" if sig_text is not None else None,
            warnings=(),
        )

    decorators: list[ast.AST] = list(getattr(node, "decorator_list", []))
    magic_index: int | None = None
    for i, dec in enumerate(decorators):
        if _is_magic_decorator(dec):
            magic_index = i
            break

    dep_set: set[SpecRef] = set()
    records: list[DecoratorApiRecord] = []
    seen_records: set[tuple[str, str, str]] = set()

    for i, dec in enumerate(decorators):
        if _is_magic_decorator(dec):
            continue

        if magic_index is None:
            position: Literal["above_magic", "below_magic"] = "above_magic"
        else:
            position = "above_magic" if i < magic_index else "below_magic"

        expr = ast.get_source_segment(source, dec)
        if not isinstance(expr, str) or not expr.strip():
            try:
                expr = ast.unparse(dec)
            except Exception:
                expr = "<decorator>"

        for symbol_path in _symbol_paths_for_decorator(dec):
            dedupe_key = (symbol_path, position, expr)
            if dedupe_key in seen_records:
                continue
            seen_records.add(dedupe_key)

            resolved = _resolve_path(module, symbol_path)
            sig_text, quality = _signature_for_obj(resolved)
            record = DecoratorApiRecord(
                symbol_path=symbol_path,
                expression=expr,
                position=position,
                resolved_target=_target_label(resolved),
                signature=sig_text,
                annotation_quality=quality,
            )
            records.append(record)

            dep_ref = _best_effort_dep_ref(resolved)
            if dep_ref is not None:
                dep_set.add(dep_ref)

    warnings: list[str] = []

    dec_sig, dec_quality = _signature_for_obj(decorated_obj)
    src_sig = _signature_from_function_node(node)

    effective_sig: str | None = None
    effective_src: Literal["decorated", "original"] | None = None
    if dec_sig is not None and dec_quality == "good":
        effective_sig = dec_sig
        effective_src = "decorated"
    elif src_sig is not None:
        effective_sig = src_sig
        effective_src = "original"
        if dec_quality in ("weak", "missing"):
            warnings.append(
                f"weak decorator type metadata for {module}:{qualname}; "
                f"using source signature instead"
            )
    elif dec_sig is not None:
        effective_sig = dec_sig
        effective_src = "decorated"

    return DecoratorAnalysis(
        auto_deps=tuple(sorted(dep_set, key=lambda x: str(x))),
        records=tuple(records),
        effective_signature=effective_sig,
        effective_signature_source=effective_src,
        warnings=tuple(warnings),
    )


def resolve_qualname_for_line(*, source_file: str, line: int) -> tuple[str, str | None] | None:
    """Best-effort mapping of source location to function/class qualname."""
    try:
        source = Path(source_file).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=source_file)
    except Exception:
        return None

    matches: list[tuple[int, int, str, str | None]] = []

    def walk(body: list[ast.stmt], prefix: str | None) -> None:
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue

            dec_lines = [
                d.lineno for d in getattr(node, "decorator_list", []) if hasattr(d, "lineno")
            ]
            start = min([node.lineno, *dec_lines])
            end = int(getattr(node, "end_lineno", node.lineno))
            if start <= line <= end:
                qual = f"{prefix}.{node.name}" if prefix else node.name
                class_name = (
                    prefix if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
                )
                depth = qual.count(".")
                span = end - start
                matches.append((depth, -span, qual, class_name))

            if isinstance(node, ast.ClassDef):
                next_prefix = f"{prefix}.{node.name}" if prefix else node.name
                walk(node.body, next_prefix)

    walk(tree.body, None)
    if not matches:
        return None
    matches.sort(reverse=True)
    _depth, _neg_span, qualname, class_name = matches[0]
    return qualname, class_name


_SIG_WARN_RE = re.compile(r"\*args|\*\*kwargs")


def signature_looks_variadic(sig_text: str | None) -> bool:
    if not sig_text:
        return False
    return bool(_SIG_WARN_RE.search(sig_text))
