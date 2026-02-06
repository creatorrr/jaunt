from __future__ import annotations

from pathlib import Path

from jaunt.deps import build_spec_graph, collapse_to_module_dag, find_cycles, toposort
from jaunt.errors import JauntDependencyCycleError
from jaunt.parse_cache import ParseCache
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _entry(
    *,
    kind: str,
    spec_ref: str,
    module: str,
    qualname: str,
    source_file: str = "/fake/source.py",
    decorator_kwargs: dict[str, object] | None = None,
) -> SpecEntry:
    return SpecEntry(
        kind=kind,  # type: ignore[arg-type]
        spec_ref=normalize_spec_ref(spec_ref),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs=decorator_kwargs or {},
    )


def test_build_spec_graph_explicit_deps_normalizes_and_ignores_missing() -> None:
    DepObj = type("DepObj", (), {})
    DepObj.__module__ = "pkg.dep"

    a = _entry(
        kind="magic",
        spec_ref="pkg.a:SpecA",
        module="pkg.a",
        qualname="SpecA",
        decorator_kwargs={
            "deps": [
                "pkg.b:SpecB",
                DepObj,  # object -> spec ref via __module__/__qualname__
                "pkg.missing:Nope",  # ignored (not in specs)
            ]
        },
    )
    b = _entry(kind="magic", spec_ref="pkg.b:SpecB", module="pkg.b", qualname="SpecB")
    dep = _entry(kind="magic", spec_ref="pkg.dep:DepObj", module="pkg.dep", qualname="DepObj")

    specs = {a.spec_ref: a, b.spec_ref: b, dep.spec_ref: dep}
    g = build_spec_graph(specs, infer_default=False)

    assert g[a.spec_ref] == {b.spec_ref, dep.spec_ref}
    assert g[b.spec_ref] == set()


def test_collapse_to_module_dag_no_self_edges_and_all_keys_present() -> None:
    a = normalize_spec_ref("m.one:A")
    b = normalize_spec_ref("m.one:B")
    c = normalize_spec_ref("m.two:C")

    spec_graph = {
        a: {b, c},  # b is same module; should collapse out; c should remain
        b: set(),
        c: set(),
    }

    mg = collapse_to_module_dag(spec_graph)
    assert set(mg.keys()) == {"m.one", "m.two"}
    assert mg["m.one"] == {"m.two"}
    assert mg["m.two"] == set()


def test_toposort_respects_dependencies() -> None:
    g = {"a": {"b", "c"}, "b": {"c"}, "c": set()}
    order = toposort(g)
    assert order.index("c") < order.index("b") < order.index("a")


def test_infer_deps_for_nested_qualname(tmp_path: Path) -> None:
    """Inference should work for nested qualnames like Class.method."""
    src = tmp_path / "mod.py"
    src.write_text(
        "class Outer:\n"
        "    def method(self) -> None:\n"
        "        Helper()\n"
        "\n"
        "def Helper():\n"
        "    pass\n",
        encoding="utf-8",
    )

    outer_method = _entry(
        kind="magic",
        spec_ref="mod:Outer.method",
        module="mod",
        qualname="Outer.method",
        source_file=str(src),
    )
    helper = _entry(
        kind="magic",
        spec_ref="mod:Helper",
        module="mod",
        qualname="Helper",
        source_file=str(src),
    )

    specs = {outer_method.spec_ref: outer_method, helper.spec_ref: helper}
    g = build_spec_graph(specs, infer_default=True)

    # The nested spec Outer.method references Helper, so it should be inferred.
    assert helper.spec_ref in g[outer_method.spec_ref]


def test_infer_deps_multi_level_attribute_chain(tmp_path: Path) -> None:
    """Inference should resolve alias.sub.Foo where alias maps to a package."""
    src = tmp_path / "mod.py"
    src.write_text(
        "import pkg.sub as ps\n\ndef caller():\n    ps.inner.Helper()\n",
        encoding="utf-8",
    )
    helper_src = tmp_path / "helper.py"
    helper_src.write_text(
        "def Helper():\n    pass\n",
        encoding="utf-8",
    )

    caller = _entry(
        kind="magic",
        spec_ref="mod:caller",
        module="mod",
        qualname="caller",
        source_file=str(src),
    )
    helper = _entry(
        kind="magic",
        spec_ref="pkg.sub.inner:Helper",
        module="pkg.sub.inner",
        qualname="Helper",
        source_file=str(helper_src),
    )

    specs = {caller.spec_ref: caller, helper.spec_ref: helper}
    g = build_spec_graph(specs, infer_default=True)

    # ps maps to pkg.sub; attribute chain is inner.Helper
    # Should try pkg.sub.inner:Helper
    assert helper.spec_ref in g[caller.spec_ref]


def test_infer_deps_follows_reexports(tmp_path: Path) -> None:
    """Inference should follow one level of re-exports through __init__.py files."""
    # Set up a package with a re-export.
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("from pkg.internal import Helper\n", encoding="utf-8")
    (pkg_dir / "internal.py").write_text("def Helper():\n    pass\n", encoding="utf-8")

    # Consumer module imports Helper from pkg (re-export).
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "from pkg import Helper\n\ndef caller():\n    Helper()\n",
        encoding="utf-8",
    )

    caller_entry = _entry(
        kind="magic",
        spec_ref="consumer:caller",
        module="consumer",
        qualname="caller",
        source_file=str(consumer),
    )
    helper_entry = _entry(
        kind="magic",
        spec_ref="pkg.internal:Helper",
        module="pkg.internal",
        qualname="Helper",
        source_file=str(pkg_dir / "internal.py"),
    )

    specs = {caller_entry.spec_ref: caller_entry, helper_entry.spec_ref: helper_entry}
    g = build_spec_graph(
        specs,
        infer_default=True,
        source_roots=[tmp_path],
    )

    # The caller imports Helper from pkg (re-export of pkg.internal:Helper).
    # Direct resolution gives pkg:Helper which isn't a known spec, but following
    # the re-export should resolve to pkg.internal:Helper.
    assert helper_entry.spec_ref in g[caller_entry.spec_ref]


def test_build_spec_graph_collects_inference_warnings(tmp_path: Path) -> None:
    """build_spec_graph should collect warnings about names it tried but failed to resolve."""
    src = tmp_path / "mod.py"
    src.write_text(
        "from unknown_pkg import Mystery\n\ndef caller():\n    Mystery()\n    Nonexistent()\n",
        encoding="utf-8",
    )
    caller = _entry(
        kind="magic",
        spec_ref="mod:caller",
        module="mod",
        qualname="caller",
        source_file=str(src),
    )
    specs = {caller.spec_ref: caller}
    warnings: list[str] = []
    g = build_spec_graph(specs, infer_default=True, warnings=warnings)

    # No deps resolved, but we should get warnings about unresolved references
    assert g[caller.spec_ref] == set()
    assert len(warnings) > 0
    assert any("Mystery" in w for w in warnings)


def test_build_spec_graph_no_warnings_when_all_resolved(tmp_path: Path) -> None:
    """No warnings should be emitted when all references resolve to known specs."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def Helper():\n    pass\n\ndef caller():\n    Helper()\n",
        encoding="utf-8",
    )
    caller = _entry(
        kind="magic",
        spec_ref="mod:caller",
        module="mod",
        qualname="caller",
        source_file=str(src),
    )
    helper = _entry(
        kind="magic",
        spec_ref="mod:Helper",
        module="mod",
        qualname="Helper",
        source_file=str(src),
    )
    specs = {caller.spec_ref: caller, helper.spec_ref: helper}
    warnings: list[str] = []
    g = build_spec_graph(specs, infer_default=True, warnings=warnings)

    assert helper.spec_ref in g[caller.spec_ref]
    assert warnings == []


def test_parse_cache_round_trips(tmp_path: Path) -> None:
    """ParseCache should parse a file once and serve it from cache on the second call."""
    src = tmp_path / "mod.py"
    src.write_text("def Foo():\n    pass\n", encoding="utf-8")

    cache = ParseCache(cache_dir=tmp_path / ".jaunt" / "cache" / "ast")
    r1 = cache.parse(str(src))
    assert r1 is not None
    source1, tree1 = r1
    assert "def Foo():" in source1

    # Second call should hit cache (same result).
    r2 = cache.parse(str(src))
    assert r2 is not None
    source2, tree2 = r2
    assert source1 == source2


def test_parse_cache_invalidates_on_source_change(tmp_path: Path) -> None:
    """ParseCache should re-parse if the file's mtime or size changed."""
    src = tmp_path / "mod.py"
    src.write_text("def Foo():\n    pass\n", encoding="utf-8")

    cache = ParseCache(cache_dir=tmp_path / ".jaunt" / "cache" / "ast")
    r1 = cache.parse(str(src))
    assert r1 is not None

    # Modify the file.
    src.write_text("def Foo():\n    return 42\n", encoding="utf-8")

    r2 = cache.parse(str(src))
    assert r2 is not None
    source2, _ = r2
    assert "return 42" in source2


def test_parse_cache_persistent_across_instances(tmp_path: Path) -> None:
    """A new ParseCache instance pointing to the same dir should find existing cache."""
    cache_dir = tmp_path / ".jaunt" / "cache" / "ast"
    src = tmp_path / "mod.py"
    src.write_text("def Foo():\n    pass\n", encoding="utf-8")

    cache1 = ParseCache(cache_dir=cache_dir)
    cache1.parse(str(src))

    # New instance with same cache_dir.
    cache2 = ParseCache(cache_dir=cache_dir)
    r = cache2.parse(str(src))
    assert r is not None
    source, _ = r
    assert "def Foo():" in source


def test_find_cycles_empty_on_acyclic_graph() -> None:
    g = {"a": {"b", "c"}, "b": {"c"}, "c": set()}
    assert find_cycles(g) == []


def test_find_cycles_detects_self_loop() -> None:
    g = {"a": {"a"}}
    cycles = find_cycles(g)
    assert len(cycles) >= 1
    assert any("a" in c for c in cycles)


def test_find_cycles_detects_two_node_cycle() -> None:
    g = {"a": {"b"}, "b": {"a"}}
    cycles = find_cycles(g)
    assert len(cycles) >= 1
    # The cycle should contain both a and b
    assert any("a" in c and "b" in c for c in cycles)


def test_find_cycles_detects_multi_node_cycle() -> None:
    g = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
    cycles = find_cycles(g)
    assert len(cycles) >= 1
    assert any("a" in c and "b" in c and "c" in c for c in cycles)


def test_find_cycles_detects_multiple_independent_cycles() -> None:
    g = {"a": {"b"}, "b": {"a"}, "c": {"d"}, "d": {"c"}, "e": set()}
    cycles = find_cycles(g)
    assert len(cycles) >= 2


def test_toposort_cycle_raises_with_participants() -> None:
    g = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
    try:
        toposort(g)
    except JauntDependencyCycleError as e:
        msg = str(e)
        assert "a" in msg and "b" in msg and "c" in msg
    else:  # pragma: no cover
        raise AssertionError("expected cycle error")
