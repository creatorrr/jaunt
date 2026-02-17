"""The tiger's mark -- stable digests for incremental rebuild decisions."""

from __future__ import annotations

import ast
import hashlib
import json
import textwrap
from pathlib import Path

from jaunt.errors import JauntDependencyCycleError
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef, normalize_spec_ref, spec_ref_from_object


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


def _normalize_deps_for_kwargs(value: object) -> list[str]:
    if value is None:
        return []

    items: list[object]
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = [value]

    out: list[str] = []
    for dep in items:
        try:
            # SpecRef is a NewType over str, so treat it as a str at runtime.
            if isinstance(dep, str):
                out.append(str(normalize_spec_ref(dep)))
            else:
                out.append(str(spec_ref_from_object(dep)))
        except Exception:
            out.append(str(dep))

    out.sort()
    return out


def local_digest(entry: SpecEntry) -> str:
    """Compute a stable sha256 for the entry's local definition + decorator kwargs."""

    seg = extract_source_segment(entry)

    kwargs: dict[str, object] = {}
    for k, v in entry.decorator_kwargs.items():
        if k == "deps":
            kwargs[k] = _normalize_deps_for_kwargs(v)
        else:
            kwargs[k] = _jsonable(v)

    stable = json.dumps(kwargs, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload = (seg + "\n" + stable).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def graph_digest(
    spec_ref: SpecRef,
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    *,
    cache: dict[SpecRef, str] | None = None,
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
        local = local_digest(specs[sr])
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
) -> str:
    """Digest for a module based on the graph_digests of its specs."""

    cache: dict[SpecRef, str] = {}
    digests: list[str] = []
    for entry in sorted(module_specs, key=lambda e: str(e.spec_ref)):
        digests.append(graph_digest(entry.spec_ref, specs, spec_graph, cache=cache))

    payload = "\n".join(sorted(digests)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
