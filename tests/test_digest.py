from __future__ import annotations

import re
from pathlib import Path

from jaunt.deps import build_spec_graph
from jaunt.digest import extract_source_segment, graph_digest, local_digest, module_digest
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _entry(
    *,
    kind: str,
    spec_ref: str,
    module: str,
    qualname: str,
    source_file: str,
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


def test_local_digest_is_deterministic_and_hex(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
def Foo():
    x = 1
    return x
""".lstrip(),
    )
    e = _entry(
        kind="magic",
        spec_ref="m:Foo",
        module="m",
        qualname="Foo",
        source_file=str(p),
        decorator_kwargs={"deps": ["m:Bar"], "infer_deps": False},
    )

    d1 = local_digest(e)
    d2 = local_digest(e)
    assert d1 == d2
    assert re.fullmatch(r"[0-9a-f]{64}", d1) is not None


def test_local_digest_changes_on_source_change(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
def Foo():
    return 1
""".lstrip(),
    )
    e = _entry(kind="magic", spec_ref="m:Foo", module="m", qualname="Foo", source_file=str(p))
    d1 = local_digest(e)

    _write(
        p,
        """
def Foo():
    return 2
""".lstrip(),
    )
    d2 = local_digest(e)
    assert d1 != d2


def test_graph_digest_changes_when_dependency_changes(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
def A():
    return 1

def B():
    return A()
""".lstrip(),
    )

    a = _entry(kind="magic", spec_ref="m:A", module="m", qualname="A", source_file=str(p))
    b = _entry(
        kind="magic",
        spec_ref="m:B",
        module="m",
        qualname="B",
        source_file=str(p),
        decorator_kwargs={"deps": ["m:A"]},
    )
    specs = {a.spec_ref: a, b.spec_ref: b}
    spec_graph = build_spec_graph(specs, infer_default=False)

    d1 = graph_digest(b.spec_ref, specs, spec_graph)

    # Update dependency source and recreate the SpecEntry so digests reflect new code.
    _write(
        p,
        """
def A():
    return 999

def B():
    return A()
""".lstrip(),
    )
    a2 = _entry(kind="magic", spec_ref="m:A", module="m", qualname="A", source_file=str(p))
    specs2 = {a2.spec_ref: a2, b.spec_ref: b}
    spec_graph2 = build_spec_graph(specs2, infer_default=False)

    d2 = graph_digest(b.spec_ref, specs2, spec_graph2)
    assert d1 != d2


def test_module_digest_is_deterministic_and_aggregates(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
def A():
    return 1

def B():
    return A()
""".lstrip(),
    )

    a = _entry(kind="magic", spec_ref="m:A", module="m", qualname="A", source_file=str(p))
    b = _entry(
        kind="magic",
        spec_ref="m:B",
        module="m",
        qualname="B",
        source_file=str(p),
        decorator_kwargs={"deps": ["m:A"]},
    )
    specs = {a.spec_ref: a, b.spec_ref: b}
    spec_graph = build_spec_graph(specs, infer_default=False)

    m1 = module_digest("m", [b, a], specs, spec_graph)
    m2 = module_digest("m", [a, b], specs, spec_graph)
    assert m1 == m2
    assert re.fullmatch(r"[0-9a-f]{64}", m1) is not None


# ---------------------------------------------------------------------------
# Method spec digest tests
# ---------------------------------------------------------------------------


def test_extract_source_segment_for_method_returns_class_source(tmp_path: Path) -> None:
    """For a method spec, extract_source_segment should return the entire class source."""
    p = tmp_path / "m.py"
    _write(
        p,
        "class MyService:\n"
        "    x: int = 0\n"
        "\n"
        "    def get_user(self, uid: int) -> dict:\n"
        '        """Get a user by ID."""\n'
        "        ...\n"
        "\n"
        "    def helper(self) -> None:\n"
        "        pass\n",
    )
    e = _entry(
        kind="magic",
        spec_ref="m:MyService.get_user",
        module="m",
        qualname="MyService.get_user",
        source_file=str(p),
    )
    seg = extract_source_segment(e)
    assert "class MyService:" in seg
    assert "def get_user" in seg
    assert "def helper" in seg
    assert "x: int = 0" in seg


def test_local_digest_for_method_changes_when_sibling_method_changes(
    tmp_path: Path,
) -> None:
    """Changing a non-magic sibling method should change the method spec's digest."""
    p = tmp_path / "m.py"
    _write(
        p,
        "class MyService:\n"
        "    def get_user(self, uid: int) -> dict:\n"
        "        ...\n"
        "\n"
        "    def helper(self) -> str:\n"
        '        return "v1"\n',
    )
    e = _entry(
        kind="magic",
        spec_ref="m:MyService.get_user",
        module="m",
        qualname="MyService.get_user",
        source_file=str(p),
    )
    d1 = local_digest(e)

    _write(
        p,
        "class MyService:\n"
        "    def get_user(self, uid: int) -> dict:\n"
        "        ...\n"
        "\n"
        "    def helper(self) -> str:\n"
        '        return "v2"\n',
    )
    d2 = local_digest(e)
    assert d1 != d2


def test_local_digest_for_method_stable_when_unchanged(tmp_path: Path) -> None:
    """Digest should be stable when the class source doesn't change."""
    p = tmp_path / "m.py"
    _write(
        p,
        "class MyService:\n    def get_user(self, uid: int) -> dict:\n        ...\n",
    )
    e = _entry(
        kind="magic",
        spec_ref="m:MyService.get_user",
        module="m",
        qualname="MyService.get_user",
        source_file=str(p),
    )
    d1 = local_digest(e)
    d2 = local_digest(e)
    assert d1 == d2
    assert re.fullmatch(r"[0-9a-f]{64}", d1) is not None
