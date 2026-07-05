import ast

from jaunt.migrate import (
    LEGACY_STUB_MIGRATION_ID,
    STUB_REEMIT_MIGRATION_ID,
    MigrationAction,
    apply_stub_rewrite,
    plan_legacy_stub_rewrites,
)

LEGACY = '''
import jaunt

@jaunt.magic()
def governed(x: int) -> int:
    """Doubles x."""
    raise RuntimeError("spec stub")

def ungoverned_helper(x: int) -> int:
    """Would become a NEW spec if rewritten."""
    raise RuntimeError("spec stub")

def unrelated_raise() -> None:
    raise RuntimeError("real error, not a stub")
'''


def test_migration_id_constants():
    assert LEGACY_STUB_MIGRATION_ID == "legacy-stub-body"
    assert STUB_REEMIT_MIGRATION_ID == "stub-reemit"


def test_plan_classifies_governed_vs_ungoverned(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(LEGACY)
    actions = plan_legacy_stub_rewrites(source_file=f, module="mod", governed_symbols={"governed"})
    by_symbol = {a.symbol: a for a in actions}
    assert by_symbol["governed"].classification == "re-stamp"
    assert by_symbol["governed"].migration_id == LEGACY_STUB_MIGRATION_ID
    assert by_symbol["governed"].kind == "rewrite-stub-body"
    assert by_symbol["governed"].module == "mod"
    assert by_symbol["ungoverned_helper"].classification == "newly-governs"
    assert "unrelated_raise" not in by_symbol


def test_apply_rewrites_body_to_ellipsis_preserving_docstring(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(LEGACY)
    [action] = [
        a
        for a in plan_legacy_stub_rewrites(
            source_file=f, module="mod", governed_symbols={"governed"}
        )
        if a.symbol == "governed"
    ]
    apply_stub_rewrite(action)
    text = f.read_text()
    assert '"""Doubles x."""' in text
    # only the ungoverned one left untouched
    assert text.count('raise RuntimeError("spec stub")') == 1
    ast.parse(text)  # still valid Python
    # the governed function now has an ellipsis body
    tree = ast.parse(text)
    governed = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "governed")
    last = governed.body[-1]
    assert isinstance(last, ast.Expr)
    assert isinstance(last.value, ast.Constant)
    assert last.value.value is Ellipsis


def test_method_level_stub_detected(tmp_path):
    src = '''
import jaunt

@jaunt.magic
class Widget:
    """A widget."""

    def render(self) -> str:
        """Render it."""
        raise RuntimeError("spec stub")
'''
    f = tmp_path / "cls.py"
    f.write_text(src)
    # A whole-class @magic governs its declared method stubs: the class name in
    # governed_symbols makes each method a re-stamp. Method actions carry the
    # dotted qualname as their symbol.
    actions = plan_legacy_stub_rewrites(source_file=f, module="cls", governed_symbols={"Widget"})
    by_symbol = {a.symbol: a for a in actions}
    assert "Widget.render" in by_symbol
    assert by_symbol["Widget.render"].classification == "re-stamp"
    apply_stub_rewrite(by_symbol["Widget.render"])
    text = f.read_text()
    assert '"""Render it."""' in text
    assert 'raise RuntimeError("spec stub")' not in text
    ast.parse(text)


def test_mixed_class_only_governed_method_restamps(tmp_path):
    # One method governed directly (dotted qualname), a sibling plain helper method
    # that is NOT governed. Only the governed one may re-stamp; the helper is
    # newly-governs and must be left alone by a default apply.
    src = '''
import jaunt

class Repo:
    def fetch(self) -> int:
        """Governed method."""
        raise RuntimeError("spec stub")

    def helper(self) -> int:
        """Plain legacy helper — rewriting would newly govern it."""
        raise RuntimeError("spec stub")
'''
    f = tmp_path / "repo.py"
    f.write_text(src)
    actions = plan_legacy_stub_rewrites(
        source_file=f, module="repo", governed_symbols={"Repo.fetch"}
    )
    by_symbol = {a.symbol: a for a in actions}
    assert by_symbol["Repo.fetch"].classification == "re-stamp"
    assert by_symbol["Repo.helper"].classification == "newly-governs"

    # Applying only the governed action rewrites exactly that one method.
    apply_stub_rewrite(by_symbol["Repo.fetch"])
    text = f.read_text()
    assert text.count('raise RuntimeError("spec stub")') == 1
    tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Repo")
    fetch = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "fetch")
    helper = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "helper")
    assert isinstance(fetch.body[-1], ast.Expr) and fetch.body[-1].value.value is Ellipsis
    assert isinstance(helper.body[-1], ast.Raise)


def test_whole_class_governs_all_method_stubs(tmp_path):
    # A whole-class governed spec (class name in governed_symbols) makes every
    # declared method stub a re-stamp.
    src = """
import jaunt

class Widget:
    def a(self) -> int:
        raise RuntimeError("spec stub")

    def b(self) -> int:
        raise RuntimeError("spec stub")
"""
    f = tmp_path / "w.py"
    f.write_text(src)
    actions = plan_legacy_stub_rewrites(source_file=f, module="w", governed_symbols={"Widget"})
    by_symbol = {a.symbol: a for a in actions}
    assert by_symbol["Widget.a"].classification == "re-stamp"
    assert by_symbol["Widget.b"].classification == "re-stamp"


def test_single_quoted_and_whitespace_variants_detected(tmp_path):
    src = """
def single_quoted() -> int:
    raise RuntimeError('spec stub')

def extra_space() -> int:
    raise RuntimeError("spec  stub")
"""
    f = tmp_path / "variants.py"
    f.write_text(src)
    actions = plan_legacy_stub_rewrites(source_file=f, module="variants", governed_symbols=set())
    by_symbol = {a.symbol: a for a in actions}
    assert "single_quoted" in by_symbol
    assert "extra_space" not in by_symbol


def test_action_is_frozen_dataclass():
    a = MigrationAction(
        migration_id=LEGACY_STUB_MIGRATION_ID,
        path=__import__("pathlib").Path("x.py"),
        module="m",
        symbol="s",
        kind="rewrite-stub-body",
        classification="re-stamp",
        description="desc",
    )
    import dataclasses

    assert dataclasses.is_dataclass(a)
    try:
        a.symbol = "y"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("MigrationAction should be frozen")
