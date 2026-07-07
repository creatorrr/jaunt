"""The `properties` case kind: Tier-1 grammar, Hypothesis rendering, reconcile wiring."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jaunt.contract.cases import CaseParseError
from jaunt.contract.derive import ContractBlocks, PropertyRow, extract_blocks_via_model
from jaunt.contract.properties import (
    PropertyBlocks,
    parse_property_blocks,
    properties_extra_imports,
    render_properties_region,
)
from jaunt.contract.runner import reconcile_entry
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref

# ---------------------------------------------------------------------------
# Tier-1 grammar
# ---------------------------------------------------------------------------


def _parse(doc: str, *, target: str = "f", async_map=None, module_names=frozenset()):
    return parse_property_blocks(
        doc, target=target, async_map=async_map or {}, module_names=module_names
    )


IDEMPOTENT = """Slugify.

Properties:
- given t: str :: f(f(t)) == f(t)
"""


def test_parse_tier1_from_type_binding() -> None:
    blocks = _parse(IDEMPOTENT)
    assert len(blocks.cases) == 1
    case = blocks.cases[0]
    assert case.bindings[0].name == "t"
    assert case.bindings[0].strategy_expr == "st.from_type(str)"
    assert case.expr == "f(f(t)) == f(t)"
    assert case.imports == ()
    assert blocks.prose == ()


def test_parse_tier1_explicit_strategy_passthrough() -> None:
    doc = "Properties:\n- given xs: st.lists(st.integers()) :: f(xs) == f(f(xs))\n"
    blocks = _parse(doc)
    assert blocks.cases[0].bindings[0].strategy_expr == "st.lists(st.integers())"


def test_parse_tier1_multiple_bindings() -> None:
    doc = "Properties:\n- given a: str, b: str :: f(a + b) == f(a) + f(b)\n"
    blocks = _parse(doc)
    assert [b.name for b in blocks.cases[0].bindings] == ["a", "b"]


def test_binding_type_name_is_classified_for_import() -> None:
    # `Path` appears only in the binding, never in the invariant (review finding).
    doc = "Properties:\n- given p: Path :: f(p) == p\n"
    blocks = _parse(doc, module_names=frozenset({"Path"}))
    assert blocks.cases[0].imports == ("Path",)
    assert properties_extra_imports(blocks) == ("Path",)


def test_compare_expression_roots_in_target() -> None:
    # The invariant is an ast.Compare, not a bare call — rooting must walk into it
    # (review finding).
    blocks = _parse(IDEMPOTENT)
    assert blocks.cases  # did not raise


def test_prose_bullet_collected_not_parsed() -> None:
    doc = "Properties:\n- Output never contains uppercase letters.\n"
    blocks = _parse(doc)
    assert blocks.cases == ()
    assert blocks.prose == ("Output never contains uppercase letters.",)


def test_mixed_structured_and_prose_bullets() -> None:
    doc = "Properties:\n- given t: str :: f(f(t)) == f(t)\n- Output is always lowercase.\n"
    blocks = _parse(doc)
    assert len(blocks.cases) == 1
    assert blocks.prose == ("Output is always lowercase.",)


def test_unrooted_invariant_raises() -> None:
    with pytest.raises(CaseParseError):
        _parse("Properties:\n- given t: str :: len(t) >= 0\n")


def test_unknown_name_raises() -> None:
    with pytest.raises(CaseParseError):
        _parse("Properties:\n- given t: str :: f(t) == mystery(t)\n")


def test_fixture_reference_rejected_with_actionable_error() -> None:
    doc = "Fixtures: db\n\nProperties:\n- given k: str :: f(db, k) == k\n"
    with pytest.raises(CaseParseError) as exc_info:
        _parse(doc)
    assert "fixture" in str(exc_info.value)


def test_async_target_rejected() -> None:
    with pytest.raises(CaseParseError):
        _parse(IDEMPOTENT, async_map={"f": True})


def test_duplicate_binding_raises() -> None:
    with pytest.raises(CaseParseError):
        _parse("Properties:\n- given t: str, t: int :: f(t) == f(f(t))\n")


def test_malformed_given_bullet_raises() -> None:
    with pytest.raises(CaseParseError):
        _parse("Properties:\n- given t str :: f(t) == f(f(t))\n")


def test_no_properties_section_is_empty() -> None:
    assert _parse("Just prose.\n").is_empty()


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_render_region_bytes() -> None:
    blocks = _parse(IDEMPOTENT)
    region = render_properties_region(blocks.cases, max_examples=25)
    assert region.region_id == "properties"
    assert "from hypothesis import given, settings" in region.code
    assert "from hypothesis import strategies as st" in region.code
    assert "@given(t=st.from_type(str))" in region.code
    assert (
        "@settings(max_examples=25, derandomize=True, database=None, deadline=None)" in region.code
    )
    assert "def test_prop_1(t):  # derived from: Properties" in region.code
    assert "    assert f(f(t)) == f(t)" in region.code


def test_render_region_suffix_names() -> None:
    blocks = _parse("Properties:\n- given n: int :: Counter(n).peek() == n\n", target="Counter")
    region = render_properties_region(blocks.cases, max_examples=10, region_suffix="peek")
    assert region.region_id == "properties-peek"
    assert "def test_prop_peek_1(n):" in region.code


# ---------------------------------------------------------------------------
# Model payload (Tier 2)
# ---------------------------------------------------------------------------


def test_extract_blocks_via_model_parses_properties() -> None:
    canned = json.dumps(
        {
            "examples": [],
            "raises": [],
            "properties": [{"bindings": "t: str", "expr": "f(f(t)) == f(t)"}],
        }
    )

    async def fake_complete(system: str, user: str) -> str:
        assert "properties" in system
        return canned

    blocks = asyncio.run(extract_blocks_via_model("x", complete=fake_complete))
    assert blocks.properties == (PropertyRow("t: str", "f(f(t)) == f(t)"),)
    assert not blocks.is_empty()


# ---------------------------------------------------------------------------
# reconcile_entry wiring
# ---------------------------------------------------------------------------

DERIVE_ALL = ["examples", "errors", "properties"]

SLUG_SRC = '''
import re

_RUN = re.compile(r"[^a-z0-9]+")


def slug(title: str) -> str:
    """Slugify.

    Examples:
        - slug("Hello World") == "hello-world"

    Properties:
    - given t: str :: slug(slug(t)) == slug(t)
    """
    return _RUN.sub("-", title.lower()).strip("-")
'''


def _project(tmp_path: Path, module_src: str) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(module_src, encoding="utf-8")
    return tmp_path


def _entry(tmp_path: Path, qualname: str, obj) -> SpecEntry:
    return SpecEntry(
        kind="contract",
        spec_ref=normalize_spec_ref(f"mod:{qualname}"),
        module="mod",
        qualname=qualname,
        source_file=str(tmp_path / "src" / "mod.py"),
        obj=obj,
        decorator_kwargs={},
    )


def _slug(title: str) -> str:
    import re

    return re.compile(r"[^a-z0-9]+").sub("-", title.lower()).strip("-")


def test_reconcile_writes_and_validates_property_battery(tmp_path: Path) -> None:
    root = _project(tmp_path, SLUG_SRC)
    res = reconcile_entry(
        root,
        "tests/contract",
        DERIVE_ALL,
        False,
        _entry(root, "slug", _slug),
        module_namespace={"slug": _slug},
        tool_version="t",
        property_max_examples=8,
    )
    assert res.ok, res.failures
    text = res.battery_path.read_text(encoding="utf-8")
    assert "# >>> jaunt:derived properties" in text
    assert "@given(t=st.from_type(str))" in text
    assert "max_examples=8" in text
    assert "def test_examples(" in text  # examples region still present


def test_reconcile_failing_property_blocks_write(tmp_path: Path) -> None:
    # Appending "-x" breaks idempotence; no Examples section so only the property
    # (validated via pytest, not evaluate_cases) can catch it.
    src = '''
def slug(title: str) -> str:
    """Slugify.

    Properties:
    - given t: str :: slug(slug(t)) == slug(t)
    """
    return title.lower() + "-x"
'''
    root = _project(tmp_path, src)

    def bad_slug(title: str) -> str:
        return title.lower() + "-x"

    res = reconcile_entry(
        root,
        "tests/contract",
        ["properties"],
        False,
        _entry(root, "slug", bad_slug),
        module_namespace={"slug": bad_slug},
        tool_version="t",
        property_max_examples=8,
    )
    assert res.ok is False
    assert any("property" in f for f in res.failures)
    assert not res.battery_path.exists()


def test_no_properties_section_is_byte_identical_across_derive_sets(tmp_path: Path) -> None:
    src = '''
def double(x: int) -> int:
    """Double.

    Examples:
        - double(2) == 4
    """
    return x * 2
'''

    def double(x: int) -> int:
        return x * 2

    texts = []
    for i, derive in enumerate((["examples", "errors"], DERIVE_ALL)):
        (tmp_path / f"p{i}").mkdir()
        root = _project(tmp_path / f"p{i}", src)
        res = reconcile_entry(
            root,
            "tests/contract",
            derive,
            False,
            _entry(root, "double", double),
            module_namespace={"double": double},
            tool_version="t",
        )
        assert res.ok, res.failures
        texts.append(res.battery_path.read_text(encoding="utf-8"))
    assert texts[0] == texts[1]


def test_prose_properties_merge_with_structured_via_model(tmp_path: Path) -> None:
    src = '''
def slug(title: str) -> str:
    """Slugify.

    Examples:
        - slug("Hello") == "hello"

    Properties:
    - given t: str :: slug(slug(t)) == slug(t)
    - Output never contains uppercase letters.
    """
    return title.lower().replace(" ", "-")
'''
    root = _project(tmp_path, src)
    seen: list[tuple[str, str]] = []

    def model_extract(prose: str, func_name: str = "f") -> ContractBlocks:
        seen.append((prose, func_name))
        return ContractBlocks(properties=(PropertyRow("t: str", "slug(t) == slug(t).lower()"),))

    def slug(title: str) -> str:
        return title.lower().replace(" ", "-")

    res = reconcile_entry(
        root,
        "tests/contract",
        DERIVE_ALL,
        False,
        _entry(root, "slug", slug),
        module_namespace={"slug": slug},
        tool_version="t",
        model_extract=model_extract,
        property_max_examples=8,
    )
    assert res.ok, res.failures
    # The model saw only the prose bullet, with the real function name.
    assert len(seen) == 1
    prose_sent, func_name_sent = seen[0]
    assert "Output never contains uppercase letters." in prose_sent
    assert "given t: str" not in prose_sent
    assert func_name_sent == "slug"
    text = res.battery_path.read_text(encoding="utf-8")
    assert "def test_prop_1(t):" in text
    assert "def test_prop_2(t):" in text


def test_properties_only_unstructured_docstring_via_model(tmp_path: Path) -> None:
    src = '''
def ident(x: int) -> int:
    """Identity: applying it twice is the same as applying it once."""
    return x
'''
    root = _project(tmp_path, src)

    def model_extract(prose: str, func_name: str = "f") -> ContractBlocks:
        return ContractBlocks(properties=(PropertyRow("x: int", "ident(ident(x)) == ident(x)"),))

    def ident(x: int) -> int:
        return x

    res = reconcile_entry(
        root,
        "tests/contract",
        DERIVE_ALL,
        False,
        _entry(root, "ident", ident),
        module_namespace={"ident": ident},
        tool_version="t",
        model_extract=model_extract,
        property_max_examples=8,
    )
    assert res.ok, res.failures
    text = res.battery_path.read_text(encoding="utf-8")
    assert "# >>> jaunt:derived properties" in text
    assert "ident(ident(x)) == ident(x)" in text


def test_strength_excluded_counts_property_cases(tmp_path: Path) -> None:
    root = _project(tmp_path, SLUG_SRC)
    res = reconcile_entry(
        root,
        "tests/contract",
        DERIVE_ALL,
        True,
        _entry(root, "slug", _slug),
        module_namespace={"slug": _slug},
        tool_version="t",
        property_max_examples=8,
    )
    assert res.ok, res.failures
    assert res.strength_excluded >= 1
    text = res.battery_path.read_text(encoding="utf-8")
    assert "jaunt:strength-excluded=" in text


CLASS_SRC = '''
class Counter:
    """A counter.

    Examples:
        - Counter(1).peek() == 1
    """

    def __init__(self, n: int) -> None:
        self.n = n

    def peek(self) -> int:
        """Return the current value.

        Properties:
        - given n: int :: Counter(n).peek() == n
        """
        return self.n
'''


def test_class_method_properties_render_with_suffix(tmp_path: Path) -> None:
    root = _project(tmp_path, CLASS_SRC)

    class Counter:
        def __init__(self, n: int) -> None:
            self.n = n

        def peek(self) -> int:
            return self.n

    res = reconcile_entry(
        root,
        "tests/contract",
        DERIVE_ALL,
        False,
        _entry(root, "Counter", Counter),
        module_namespace={"Counter": Counter},
        tool_version="t",
        property_max_examples=8,
    )
    assert res.ok, res.failures
    text = res.battery_path.read_text(encoding="utf-8")
    assert "# >>> jaunt:derived properties-peek" in text
    assert "def test_prop_peek_1(n):" in text


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_accepts_properties_and_budget(tmp_path: Path) -> None:
    from jaunt.config import load_config

    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n\n[contract]\nderive = ["examples", "properties"]\n'
        "property_max_examples = 25\n",
        encoding="utf-8",
    )
    cfg = load_config(root=tmp_path)
    assert cfg.contract.derive == ["examples", "properties"]
    assert cfg.contract.property_max_examples == 25


def test_config_rejects_nonpositive_budget(tmp_path: Path) -> None:
    from jaunt.config import load_config
    from jaunt.errors import JauntConfigError

    (tmp_path / "jaunt.toml").write_text(
        "version = 1\n\n[contract]\nproperty_max_examples = 0\n", encoding="utf-8"
    )
    with pytest.raises(JauntConfigError):
        load_config(root=tmp_path)


def test_property_blocks_merged() -> None:
    a = _parse("Properties:\n- given t: str :: f(f(t)) == f(t)\n")
    b = PropertyBlocks(prose=("something",))
    merged = a.merged(b)
    assert len(merged.cases) == 1
    assert merged.prose == ("something",)
