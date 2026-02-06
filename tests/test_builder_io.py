from __future__ import annotations

from pathlib import Path

from jaunt.builder import detect_stale_modules, write_generated_module
from jaunt.deps import build_spec_graph
from jaunt.digest import module_digest
from jaunt.header import HEADER_MARKER
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(*, module: str, qualname: str, source_file: str) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs={},
    )


def test_write_generated_module_creates_header_and_inits_and_overwrites(tmp_path: Path) -> None:
    src = tmp_path / "src"

    spec_path = tmp_path / "pkg" / "specs.py"
    _write(
        spec_path,
        """
def Foo():
    return 1
""".lstrip(),
    )
    e = _entry(module="pkg.specs", qualname="Foo", source_file=str(spec_path))
    specs = {e.spec_ref: e}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.specs": [e]}

    stale = detect_stale_modules(
        package_dir=src,
        generated_dir="__generated__",
        module_specs=module_specs,
        specs=specs,
        spec_graph=spec_graph,
    )
    assert stale == {"pkg.specs"}

    d = module_digest("pkg.specs", [e], specs, spec_graph)
    out = write_generated_module(
        package_dir=src,
        generated_dir="__generated__",
        module_name="pkg.specs",
        source="def Foo():\n    return 123\n",
        header_fields={
            "tool_version": "0",
            "kind": "build",
            "source_module": "pkg.specs",
            "module_digest": d,
            "spec_refs": [str(e.spec_ref)],
        },
    )
    assert out.exists()
    txt = out.read_text(encoding="utf-8")
    assert txt.startswith(HEADER_MARKER)
    assert "def Foo():" in txt

    # Intermediate __init__.py files exist for importability.
    assert (src / "pkg" / "__init__.py").exists()
    assert (src / "pkg" / "__generated__" / "__init__.py").exists()

    # Overwrite: last content should win.
    write_generated_module(
        package_dir=src,
        generated_dir="__generated__",
        module_name="pkg.specs",
        source="def Foo():\n    return 999\n",
        header_fields={
            "tool_version": "0",
            "kind": "build",
            "source_module": "pkg.specs",
            "module_digest": d,
            "spec_refs": [str(e.spec_ref)],
        },
    )
    txt2 = out.read_text(encoding="utf-8")
    assert "return 999" in txt2

    stale2 = detect_stale_modules(
        package_dir=src,
        generated_dir="__generated__",
        module_specs=module_specs,
        specs=specs,
        spec_graph=spec_graph,
    )
    assert stale2 == set()


def test_detect_stale_modules_force_and_digest_change(tmp_path: Path) -> None:
    src = tmp_path / "src"
    spec_path = tmp_path / "m.py"
    _write(
        spec_path,
        """
def A():
    return 1
""".lstrip(),
    )
    e = _entry(module="m", qualname="A", source_file=str(spec_path))
    specs = {e.spec_ref: e}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"m": [e]}

    assert detect_stale_modules(
        package_dir=src,
        generated_dir="__generated__",
        module_specs=module_specs,
        specs=specs,
        spec_graph=spec_graph,
        force=True,
    ) == {"m"}

    d1 = module_digest("m", [e], specs, spec_graph)
    out = write_generated_module(
        package_dir=src,
        generated_dir="__generated__",
        module_name="m",
        source="def A():\n    return 1\n",
        header_fields={
            "tool_version": "0",
            "kind": "build",
            "source_module": "m",
            "module_digest": d1,
            "spec_refs": [str(e.spec_ref)],
        },
    )
    assert out.exists()

    # Modify the spec source to change the digest; should become stale.
    _write(
        spec_path,
        """
def A():
    return 2
""".lstrip(),
    )
    e2 = _entry(module="m", qualname="A", source_file=str(spec_path))
    specs2 = {e2.spec_ref: e2}
    spec_graph2 = build_spec_graph(specs2, infer_default=False)
    module_specs2 = {"m": [e2]}

    stale = detect_stale_modules(
        package_dir=src,
        generated_dir="__generated__",
        module_specs=module_specs2,
        specs=specs2,
        spec_graph=spec_graph2,
    )
    assert stale == {"m"}
