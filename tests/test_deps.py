from __future__ import annotations

from jaunt.deps import build_spec_graph, collapse_to_module_dag, toposort
from jaunt.errors import JauntDependencyCycleError
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _entry(
    *,
    kind: str,
    spec_ref: str,
    module: str,
    qualname: str,
    decorator_kwargs: dict[str, object] | None = None,
) -> SpecEntry:
    return SpecEntry(
        kind=kind,  # type: ignore[arg-type]
        spec_ref=normalize_spec_ref(spec_ref),
        module=module,
        qualname=qualname,
        source_file="/fake/source.py",
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


def test_toposort_cycle_raises_with_participants() -> None:
    g = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
    try:
        toposort(g)
    except JauntDependencyCycleError as e:
        msg = str(e)
        assert "a" in msg and "b" in msg and "c" in msg
    else:  # pragma: no cover
        raise AssertionError("expected cycle error")
