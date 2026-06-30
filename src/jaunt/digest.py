"""The tiger's mark -- stable digests for incremental rebuild decisions."""

from __future__ import annotations

import ast
import hashlib
import json
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jaunt.class_analysis import is_preserve_decorator, is_stub_body, split_class_members
from jaunt.errors import JauntDependencyCycleError
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef, normalize_spec_refs


def extract_source_segment(entry: SpecEntry) -> str:
    """Extract a normalized source segment for the entry's definition.

    For top-level definitions, extracts the function/class node.
    For method specs (dotted qualname), extracts the **entire enclosing class**
    so that the digest covers sibling changes and the LLM gets full context.
    """

    src = Path(entry.source_file).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=entry.source_file)

    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | None = None

    if "." in entry.qualname:
        # Method spec: extract the enclosing class.
        class_name = entry.qualname.split(".")[0]
        for top in tree.body:
            if isinstance(top, ast.ClassDef) and top.name == class_name:
                node = top
                break
    else:
        for top in tree.body:
            if (
                isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and top.name == entry.qualname
            ):
                node = top
                break

    if node is None:
        if "." in entry.qualname:
            class_name = entry.qualname.split(".")[0]
            raise ValueError(f"Enclosing class {class_name!r} not found for {entry.spec_ref!s}")
        raise ValueError(f"Top-level definition not found for {entry.spec_ref!s}")

    seg = ast.get_source_segment(src, node)
    if seg is None:
        raise ValueError(f"Unable to extract source for {entry.spec_ref!s}")

    seg = textwrap.dedent(seg)
    seg = seg.replace("\r\n", "\n").replace("\r", "\n")

    # Strip trailing whitespace (per-line) and trim trailing blank lines for stability.
    lines = [line.rstrip() for line in seg.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        # JSON keys must be strings; coerce to str for stability.
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(v) for v in value), key=lambda x: str(x))
    return str(value)


def _normalize_spec_refs_for_kwargs(value: object) -> list[str]:
    if value is None:
        return []
    try:
        return [str(ref) for ref in normalize_spec_refs(value)]
    except Exception:
        pass

    items: list[object]
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = [value]
    out = [str(item) for item in items]
    out.sort()
    return out


@dataclass(frozen=True, slots=True)
class NormalizedContract:
    ref: str
    kind: str
    signature: str
    decorator_meta: str
    prose: str
    body: str
    members: str


def normalized_contract(entry: SpecEntry) -> NormalizedContract:
    seg = extract_source_segment(entry)
    node = ast.parse(seg).body[0]
    ref = str(entry.spec_ref)
    is_method = "." in entry.qualname

    cls_node = node if isinstance(node, ast.ClassDef) else None
    target_method = _find_target_method(cls_node, entry.qualname) if is_method else None

    if is_method:
        kind = _method_kind(target_method)
        primary_node = target_method
        function_node = target_method
    elif isinstance(node, ast.ClassDef):
        kind = "class"
        primary_node = node
        function_node = None
    elif isinstance(node, ast.AsyncFunctionDef):
        kind = "async_function"
        primary_node = node
        function_node = node
    else:
        kind = "function"
        primary_node = node if isinstance(node, ast.FunctionDef) else None
        function_node = primary_node

    if entry.effective_signature is not None:
        signature = entry.effective_signature
    elif kind == "class":
        signature = ""
    else:
        signature = _function_signature(function_node)

    decorator_meta = _decorator_meta(entry, primary_node)
    if primary_node is None:
        prose = ""
    else:
        prose = ast.get_docstring(primary_node, clean=True) or ""
    body = "" if kind == "class" else _normalized_function_body(function_node)
    if cls_node is not None and (kind == "class" or is_method):
        members = _normalized_members(cls_node)
    else:
        members = ""

    return NormalizedContract(
        ref=ref,
        kind=kind,
        signature=signature,
        decorator_meta=decorator_meta,
        prose=prose,
        body=body,
        members=members,
    )


def structural_digest(entry: SpecEntry) -> str:
    contract = normalized_contract(entry)
    return _sha(
        _stable_contract_payload(
            {
                "ref": contract.ref,
                "kind": contract.kind,
                "signature": contract.signature,
                "decorator_meta": contract.decorator_meta,
                "body": contract.body,
                "members": contract.members,
            }
        )
    )


def prose_digest(entry: SpecEntry) -> str:
    return _sha(normalized_contract(entry).prose)


def local_digest(entry: SpecEntry) -> str:
    """Compute a stable sha256 for the entry's normalized local contract."""

    contract = normalized_contract(entry)
    return _sha(
        _stable_contract_payload(
            {
                "ref": contract.ref,
                "kind": contract.kind,
                "signature": contract.signature,
                "decorator_meta": contract.decorator_meta,
                "prose": contract.prose,
                "body": contract.body,
                "members": contract.members,
            }
        )
    )


def contract_snapshot(entry: SpecEntry) -> dict:
    contract = normalized_contract(entry)
    structural_payload = _stable_contract_payload(
        {
            "ref": contract.ref,
            "kind": contract.kind,
            "signature": contract.signature,
            "decorator_meta": contract.decorator_meta,
            "body": contract.body,
            "members": contract.members,
        }
    )
    return {
        "kind": contract.kind,
        "signature": contract.signature,
        "decorator_meta": contract.decorator_meta,
        "prose": contract.prose,
        "structural_digest": _sha(structural_payload),
        "prose_digest": _sha(contract.prose),
    }


def _stable_decorator_kwargs(entry: SpecEntry) -> str:
    kwargs: dict[str, object] = {}
    for k, v in entry.decorator_kwargs.items():
        if k in {"deps", "targets"}:
            kwargs[k] = _normalize_spec_refs_for_kwargs(v)
        else:
            kwargs[k] = _jsonable(v)

    return json.dumps(kwargs, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stable_contract_payload(payload: dict[str, str]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _find_target_method(
    cls_node: ast.ClassDef | None,
    qualname: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    if cls_node is None:
        return None
    target_name = qualname.split(".")[-1]
    for child in cls_node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == target_name:
            return child
    return None


def _method_kind(node: ast.FunctionDef | ast.AsyncFunctionDef | None) -> str:
    return "async_method" if isinstance(node, ast.AsyncFunctionDef) else "method"


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef | None) -> str:
    if node is None:
        return ""
    return ast.unparse(node.args) + " -> " + (ast.unparse(node.returns) if node.returns else "")


def _decorator_meta(
    entry: SpecEntry,
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> str:
    stable_kwargs = _stable_decorator_kwargs(entry)
    decorators = []
    if node is not None:
        decorators = sorted(
            ast.unparse(dec) for dec in node.decorator_list if not _is_jaunt_decorator(dec)
        )
    return stable_kwargs + "\n" + "\n".join(decorators)


def _is_jaunt_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "jaunt"
            and target.attr in {"magic", "preserve", "test", "contract"}
        )
    if isinstance(target, ast.Name):
        return target.id in {"magic", "preserve", "test", "contract"}
    return False


def _normalized_function_body(node: ast.FunctionDef | ast.AsyncFunctionDef | None) -> str:
    if node is None or is_stub_body(node):
        return ""
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _normalized_members(cls_node: ast.ClassDef) -> str:
    split = split_class_members(cls_node)
    methods = {
        n.name: n for n in cls_node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    preserved = set(split.preserved)
    class_attributes: dict[str, str] = {}

    for node in cls_node.body:
        if isinstance(node, ast.Assign):
            rendered = ast.unparse(node)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    class_attributes[target.id] = rendered
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            class_attributes[node.target.id] = ast.unparse(node)

    method_contracts: dict[str, dict[str, str]] = {}
    for name, method in methods.items():
        record = {
            "kind": _method_kind(method),
            "signature": _function_signature(method),
            "docstring": ast.get_docstring(method, clean=True) or "",
            "body": "",
        }
        if name in preserved:
            record["body"] = _normalized_preserved_method(method)
        method_contracts[name] = record

    members = {
        "class_name": cls_node.name,
        "bases": [ast.unparse(b) for b in cls_node.bases],
        "keywords": [ast.unparse(k) for k in cls_node.keywords],
        "class_decorators": sorted(
            ast.unparse(d) for d in cls_node.decorator_list if not _is_jaunt_decorator(d)
        ),
        "class_attributes": class_attributes,
        "methods": method_contracts,
    }
    return json.dumps(members, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _normalized_preserved_method(method: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    clone = ast.parse(ast.unparse(method)).body[0]
    if not isinstance(clone, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    clone.decorator_list = [d for d in clone.decorator_list if not is_preserve_decorator(d)]
    return ast.unparse(clone)


def graph_digest(
    spec_ref: SpecRef,
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    *,
    cache: dict[SpecRef, str] | None = None,
    local_fn: Callable[[SpecEntry], str] = local_digest,
) -> str:
    """Digest for a spec including transitive dependency digests (memoized)."""

    memo: dict[SpecRef, str] = cache if cache is not None else {}
    visiting: set[SpecRef] = set()

    def compute(sr: SpecRef) -> str:
        if sr in memo:
            return memo[sr]
        if sr in visiting:
            raise JauntDependencyCycleError(f"Dependency cycle detected while hashing: {sr!s}")

        visiting.add(sr)
        local = local_fn(specs[sr])
        dep_digests = [
            compute(dep) for dep in sorted(spec_graph.get(sr, set()), key=lambda x: str(x))
        ]
        payload = (local + "\n" + "\n".join(dep_digests)).encode("utf-8")
        d = hashlib.sha256(payload).hexdigest()
        memo[sr] = d
        visiting.remove(sr)
        return d

    return compute(spec_ref)


def module_digest(
    module_name: str,
    module_specs: list[SpecEntry],
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    *,
    local_fn: Callable[[SpecEntry], str] = local_digest,
) -> str:
    """Digest for a module based on the graph_digests of its specs."""

    cache: dict[SpecRef, str] = {}
    digests: list[str] = []
    for entry in sorted(module_specs, key=lambda e: str(e.spec_ref)):
        digests.append(
            graph_digest(entry.spec_ref, specs, spec_graph, cache=cache, local_fn=local_fn)
        )

    payload = "\n".join(sorted(digests)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def legacy_local_digest(entry: SpecEntry) -> str:
    """Pre-scheme-2 local digest: raw source segment + normalized decorator kwargs.

    Kept only so the migration escape hatch can recompute the *old* (scheme-1)
    module digest and recognize a generated file as genuinely fresh under the old
    scheme. Must stay byte-identical to the pre-normalization ``local_digest``.
    """

    seg = extract_source_segment(entry)
    return _sha(seg + "\n" + _stable_decorator_kwargs(entry))


def legacy_module_digest(
    module_name: str,
    module_specs: list[SpecEntry],
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
) -> str:
    """Recompute the old (scheme-1) module digest for migration detection."""

    return module_digest(module_name, module_specs, specs, spec_graph, local_fn=legacy_local_digest)


@dataclass(frozen=True, slots=True)
class ContractDigests:
    prose: str
    signature: str
    body: str


def load_function_node(source_file: str, qualname: str) -> ast.FunctionDef:
    """Load a top-level sync function node by name (v1: no classes/methods/async)."""

    if "." in qualname:
        raise ValueError(f"Contract specs must be top-level functions in v1, got {qualname!r}.")
    src = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=source_file)
    for top in tree.body:
        if isinstance(top, ast.AsyncFunctionDef) and top.name == qualname:
            raise ValueError(f"Contract function {qualname!r} is async; unsupported in v1.")
        if isinstance(top, ast.FunctionDef) and top.name == qualname:
            return top
    raise ValueError(f"Top-level function {qualname!r} not found in {source_file}.")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def contract_digests(source_file: str, qualname: str) -> ContractDigests:
    """Compute stable prose/signature/body digests for a contract function.

    - prose: the cleaned docstring (PEP-257), or "" if absent.
    - signature: AST-unparsed argument list + return annotation (normalizes formatting).
    - body: AST-unparsed body with the docstring statement stripped (normalizes
      comments/whitespace; changes only when the executable body changes).
    """

    node = load_function_node(source_file, qualname)
    prose = ast.get_docstring(node, clean=True) or ""

    sig = ast.unparse(node.args) + " -> " + (ast.unparse(node.returns) if node.returns else "")

    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    body_src = "\n".join(ast.unparse(stmt) for stmt in body)

    return ContractDigests(prose=_sha(prose), signature=_sha(sig), body=_sha(body_src))
