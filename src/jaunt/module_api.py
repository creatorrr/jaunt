"""Public API summaries for generated spec modules."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef


@dataclass(frozen=True, slots=True)
class ApiMemberSummary:
    kind: str
    name: str
    signature: str
    doc: str = ""

    def to_payload(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "name": self.name,
            "signature": self.signature,
            "doc": self.doc,
        }

    def to_prompt_lines(self, *, indent: str = "") -> list[str]:
        lines = [f"{indent}kind: {self.kind}", f"{indent}name: {self.name}"]
        if self.signature:
            lines.append(f"{indent}signature: {self.signature}")
        _append_doc_lines(lines, self.doc, indent=indent)
        return lines


@dataclass(frozen=True, slots=True)
class SpecApiSummary:
    spec_ref: SpecRef
    kind: str
    name: str
    signature: str
    doc: str
    class_name: str | None = None
    members: tuple[ApiMemberSummary, ...] = ()

    def to_prompt_block(self) -> str:
        lines = [f"kind: {self.kind}"]
        if self.class_name:
            lines.append(f"class: {self.class_name}")
        if self.signature:
            lines.append(f"signature: {self.signature}")
        _append_doc_lines(lines, self.doc)
        for member in self.members:
            lines.append("member:")
            lines.extend(member.to_prompt_lines(indent="  "))
        return "\n".join(lines)


def build_spec_api_summary(entry: SpecEntry) -> SpecApiSummary:
    source = Path(entry.source_file).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=entry.source_file)

    if "." in entry.qualname:
        class_name, _, method_name = entry.qualname.partition(".")
        class_node = _find_top_level_class(tree, class_name)
        if class_node is None:
            raise ValueError(f"Enclosing class {class_name!r} not found for {entry.spec_ref!s}")
        method_node = _find_method_node(class_node, method_name)
        if method_node is None:
            raise ValueError(f"Method {entry.qualname!r} not found for {entry.spec_ref!s}")
        signature = entry.effective_signature or _signature_line(source, method_node)
        return SpecApiSummary(
            spec_ref=entry.spec_ref,
            kind="method",
            name=method_name,
            class_name=class_name,
            signature=signature,
            doc=_doc_text(method_node),
        )

    top = _find_top_level_node(tree, entry.qualname)
    if top is None:
        raise ValueError(f"Top-level definition not found for {entry.spec_ref!s}")

    if isinstance(top, ast.ClassDef):
        return SpecApiSummary(
            spec_ref=entry.spec_ref,
            kind="class",
            name=top.name,
            signature=_class_signature(top),
            doc=_doc_text(top),
            members=_class_members(source, top),
        )

    kind = "async_function" if isinstance(top, ast.AsyncFunctionDef) else "function"
    signature = entry.effective_signature or _signature_line(source, top)
    return SpecApiSummary(
        spec_ref=entry.spec_ref,
        kind=kind,
        name=top.name,
        signature=signature,
        doc=_doc_text(top),
    )


def build_dependency_api_block(entry: SpecEntry) -> str:
    return build_spec_api_summary(entry).to_prompt_block()


def module_api_digest(module_specs: list[SpecEntry]) -> str:
    payload = [
        {
            "spec_ref": str(summary.spec_ref),
            "kind": summary.kind,
            "name": summary.name,
            "signature": summary.signature,
            "doc": summary.doc,
            "class_name": summary.class_name,
            "members": [member.to_payload() for member in summary.members],
        }
        for summary in sorted(
            (build_spec_api_summary(entry) for entry in module_specs),
            key=lambda summary: str(summary.spec_ref),
        )
    ]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _find_top_level_node(
    tree: ast.Module,
    qualname: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
            node.name == qualname
        ):
            return node
    return None


def _find_top_level_class(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _find_method_node(
    class_node: ast.ClassDef,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
            return node
    return None


def _signature_line(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    seg = ast.get_source_segment(source, node) or ""
    first = next((line.strip() for line in seg.splitlines() if line.strip()), "")
    if first.startswith("@"):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}(...)"
    if first:
        return first.removesuffix(":")
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}(...)"


def _class_signature(node: ast.ClassDef) -> str:
    bases = ", ".join(ast.unparse(base) for base in node.bases)
    if bases:
        return f"class {node.name}({bases})"
    return f"class {node.name}"


def _class_members(source: str, class_node: ast.ClassDef) -> tuple[ApiMemberSummary, ...]:
    members: list[ApiMemberSummary] = []
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef):
            members.append(
                ApiMemberSummary(
                    kind="method",
                    name=node.name,
                    signature=_signature_line(source, node),
                    doc=_doc_text(node),
                )
            )
            continue
        if isinstance(node, ast.AsyncFunctionDef):
            members.append(
                ApiMemberSummary(
                    kind="async_method",
                    name=node.name,
                    signature=_signature_line(source, node),
                    doc=_doc_text(node),
                )
            )
            continue
        if isinstance(node, ast.ClassDef):
            members.append(
                ApiMemberSummary(
                    kind="nested_class",
                    name=node.name,
                    signature=_class_signature(node),
                    doc=_doc_text(node),
                )
            )
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for name in _assignment_names(node):
                members.append(
                    ApiMemberSummary(
                        kind="class_attribute",
                        name=name,
                        signature=_assignment_signature(node, name),
                    )
                )
    return tuple(
        sorted(
            members,
            key=lambda member: (member.kind, member.name, member.signature, member.doc),
        )
    )


def _doc_text(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> str:
    doc = ast.get_docstring(node, clean=True)
    return doc or ""


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


def _assignment_signature(node: ast.Assign | ast.AnnAssign | ast.AugAssign, name: str) -> str:
    if isinstance(node, ast.AnnAssign):
        return f"{name}: {ast.unparse(node.annotation)}"
    return name


def _append_doc_lines(lines: list[str], doc: str, *, indent: str = "") -> None:
    if not doc:
        return
    doc_lines = doc.splitlines()
    if len(doc_lines) == 1:
        lines.append(f"{indent}doc: {doc_lines[0]}")
        return
    lines.append(f"{indent}doc:")
    for line in doc_lines:
        lines.append(f"{indent}  {line}")
