"""Static analysis of a @magic class body: modes, stub heuristic, member split."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from typing import Literal


def is_preserve_decorator(dec: ast.expr) -> bool:
    """True for ``@jaunt.preserve``, ``@preserve``, or their called forms."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "jaunt"
            and target.attr == "preserve"
        )
    if isinstance(target, ast.Name):
        return target.id == "preserve"
    return False


def is_magic_decorator(dec: ast.expr) -> bool:
    """True for ``@jaunt.magic``/``@magic`` and their called forms (local copy to keep
    this module dependency-free)."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "jaunt"
            and target.attr == "magic"
        )
    if isinstance(target, ast.Name):
        return target.id == "magic"
    return False


_is_magic_decorator = is_magic_decorator


def _is_property_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id == "property"
    if isinstance(target, ast.Attribute):
        return target.attr in {"setter", "getter", "deleter"}
    return False


def is_stub_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the body is only docstring / ``...`` / ``pass`` / ``raise NotImplementedError``."""
    for stmt in node.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            # docstring or a bare ``...`` (Ellipsis is a Constant in 3.8+).
            continue
        if isinstance(stmt, ast.Raise) and _is_not_implemented(stmt):
            continue
        return False
    return True


def _is_not_implemented(node: ast.Raise) -> bool:
    exc = node.exc
    if exc is None:
        return False
    if isinstance(exc, ast.Name):
        return exc.id == "NotImplementedError"
    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
        return exc.func.id == "NotImplementedError"
    return False


@dataclass(frozen=True, slots=True)
class MemberSplit:
    stubs: tuple[str, ...]
    sealed: tuple[str, ...]
    preserved: tuple[str, ...]
    preserve_marked: tuple[str, ...]


def _iter_methods(
    class_node: ast.ClassDef,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in class_node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def split_class_members(class_node: ast.ClassDef) -> MemberSplit:
    from jaunt.errors import JauntError

    stubs: list[str] = []
    sealed: list[str] = []
    preserved: list[str] = []
    preserve_marked: list[str] = []
    for fn in _iter_methods(class_node):
        marked = any(is_preserve_decorator(d) for d in fn.decorator_list)
        magic_marked = any(is_magic_decorator(d) for d in fn.decorator_list)
        if magic_marked:
            if marked:
                raise JauntError(
                    f"{class_node.name}.{fn.name}: @jaunt.magic and @jaunt.preserve are "
                    "contradictory tiers; use exactly one."
                )
            if any(_is_property_decorator(d) for d in fn.decorator_list):
                raise JauntError(
                    f"{class_node.name}.{fn.name}: @property cannot be sealed with inner "
                    "@jaunt.magic (v1); leave it as a guidepost stub or hand-write it "
                    "with @jaunt.preserve."
                )
            if not is_stub_body(fn):
                raise JauntError(
                    f"{class_node.name}.{fn.name}: inner @jaunt.magic on a hand-written "
                    "body; use @jaunt.preserve to keep it, or reduce it to a stub for "
                    "Jaunt to implement."
                )
            sealed.append(fn.name)
            stubs.append(fn.name)
        elif marked:
            preserve_marked.append(fn.name)
            preserved.append(fn.name)
        elif is_stub_body(fn):
            stubs.append(fn.name)
        else:
            preserved.append(fn.name)
    return MemberSplit(
        stubs=tuple(sorted(stubs)),
        sealed=tuple(sorted(sealed)),
        preserved=tuple(sorted(preserved)),
        preserve_marked=tuple(sorted(preserve_marked)),
    )


def classify_class_mode(class_node: ast.ClassDef) -> Literal["docstring_only", "stubs", "mix"]:
    methods = _iter_methods(class_node)
    if not methods:
        return "docstring_only"
    split = split_class_members(class_node)
    if split.stubs and not split.preserved:
        return "stubs"
    if not split.stubs and split.preserved:
        return "mix"  # all-real class under @magic is still "mix" (nothing to generate but bodies)
    return "mix"


@dataclass(frozen=True, slots=True)
class BaseContract:
    block: str
    project_base_refs: tuple[str, ...]
    required_abstractmethods: tuple[str, ...]


def resolve_base_contract(cls_obj: type) -> BaseContract:
    required = tuple(sorted(getattr(cls_obj, "__abstractmethods__", frozenset())))

    project_refs: list[str] = []
    for base in cls_obj.__bases__:
        if base is object:
            continue
        mod = getattr(base, "__module__", "")
        qual = getattr(base, "__qualname__", base.__name__)
        # A project base is any non-stdlib base; record a spec-ref-shaped string.
        if mod and not mod.startswith(("builtins", "abc", "typing", "collections")):
            project_refs.append(f"{mod}:{qual}")

    lines: list[str] = []
    seen: set[str] = set()
    for base in cls_obj.__mro__[1:]:
        if base is object:
            continue
        for name, member in sorted(vars(base).items()):
            if name.startswith("_") and not name.startswith("__"):
                continue
            if name in seen or not callable(member):
                continue
            seen.add(name)
            try:
                sig = str(inspect.signature(member))
            except (TypeError, ValueError):
                sig = "(...)"
            abstract = " [abstractmethod]" if name in required else ""
            lines.append(f"{base.__name__}.{name}{sig}{abstract}")

    block = "\n".join(lines) if lines else "(no base classes)"
    return BaseContract(
        block=block,
        project_base_refs=tuple(project_refs),
        required_abstractmethods=required,
    )


_IMPLEMENT_SENTINEL = "# jaunt:implement"


def collect_spec_module_imports(spec_source: str) -> list[str]:
    """Every top-level import / from-import in the spec module, unparsed, in order.

    Unlike preamble extraction this does not stop at the first decorated def, so an
    import that only a preserved method or class decorator needs is not dropped.
    """
    try:
        mod = ast.parse(spec_source or "")
    except SyntaxError:
        return []
    out: list[str] = []
    for node in mod.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            rendered = ast.unparse(node).strip()
            if rendered:
                out.append(rendered)
    return out


def _stub_node_with_sentinel(
    node: ast.FunctionDef | ast.AsyncFunctionDef, class_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    clone = ast.parse(ast.unparse(node)).body[0]
    assert isinstance(clone, (ast.FunctionDef, ast.AsyncFunctionDef))
    clone.decorator_list = [d for d in clone.decorator_list if not is_magic_decorator(d)]
    body: list[ast.stmt] = []
    doc = ast.get_docstring(node, clean=False)
    if doc is not None:
        body.append(ast.Expr(value=ast.Constant(value=doc)))
    msg = f"jaunt: implement {class_name}.{node.name} per the spec"
    body.append(ast.parse(f"raise NotImplementedError({msg!r})").body[0])
    clone.body = body
    return clone


def _attach_sentinels(text: str) -> str:
    """Re-attach the ``# jaunt:implement`` comment that ``ast.unparse`` drops."""
    out: list[str] = []
    for line in text.splitlines():
        if (
            "raise NotImplementedError" in line
            and "jaunt: implement" in line
            and _IMPLEMENT_SENTINEL not in line
        ):
            line = f"{line}  {_IMPLEMENT_SENTINEL}"
        out.append(line)
    return "\n".join(out)


def build_class_scaffold(class_segment: str) -> str:
    """Aider seed scaffold for a single whole-class @magic spec (see module docstring)."""
    cls = ast.parse(class_segment).body[0]
    assert isinstance(cls, ast.ClassDef)
    class_name = cls.name

    new_body: list[ast.stmt] = []
    doc = ast.get_docstring(cls, clean=False)
    if doc is not None:
        new_body.append(ast.Expr(value=ast.Constant(value=doc)))
    for node in cls.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            new_body.append(node)

    split = split_class_members(cls)
    methods = {n.name: n for n in _iter_methods(cls)}

    for name in split.preserved:
        clone = ast.parse(ast.unparse(methods[name])).body[0]
        assert isinstance(clone, (ast.FunctionDef, ast.AsyncFunctionDef))
        clone.decorator_list = [
            d
            for d in clone.decorator_list
            if not (is_preserve_decorator(d) or is_magic_decorator(d))
        ]
        new_body.append(clone)

    for name in split.stubs:
        new_body.append(_stub_node_with_sentinel(methods[name], class_name))

    # Emit `pass` only when the class declares no methods (docstring-only / attrs-only),
    # regardless of a docstring already being present (Codex finding #1).
    if not split.stubs and not split.preserved:
        new_body.append(ast.Pass())

    new_cls = ast.ClassDef(
        name=class_name,
        bases=cls.bases,
        keywords=cls.keywords,
        body=new_body,
        decorator_list=[d for d in cls.decorator_list if not _is_magic_decorator(d)],
        type_params=getattr(cls, "type_params", []),
    )
    ast.fix_missing_locations(new_cls)
    return _attach_sentinels(ast.unparse(new_cls)).rstrip() + "\n"


def render_whole_class_contract(*, class_segment: str, base_contract_block: str) -> str:
    cls = ast.parse(class_segment).body[0]
    assert isinstance(cls, ast.ClassDef)
    split = split_class_members(cls)
    mode = classify_class_mode(cls)

    lines = [f"# Whole-class generation contract: {cls.name}", ""]
    if split.stubs:
        lines.append(
            "Replace each `# jaunt:implement` method body with a real implementation "
            "(remove the sentinel and the NotImplementedError):"
        )
        lines.extend(f"- {cls.name}.{name}" for name in split.stubs)
        lines.append("")
    if split.preserved:
        lines.append("Keep these methods EXACTLY as written — do not modify their bodies:")
        lines.extend(f"- {cls.name}.{name}" for name in split.preserved)
        lines.append("")
    if mode == "docstring_only":
        lines.append(
            "Design the full public API the class docstring implies; define real public "
            "methods (an empty class body is invalid)."
        )
        lines.append("")
    block = base_contract_block.strip()
    if block and block != "(no base classes)":
        lines.append(
            "Base-class / abstractmethod contract — implement all inherited "
            "abstractmethods and keep overrides signature-compatible:"
        )
        lines.append(block)
        lines.append("")
    lines.extend(
        [
            "Retain the class docstring (you may add to it).",
            "Preserve declared base classes, class decorators, and class attributes verbatim.",
            "You may add `__init__`, private helpers, and shared state as needed.",
        ]
    )
    return "\n".join(lines) + "\n"
