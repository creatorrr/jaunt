from __future__ import annotations

from pathlib import Path

from jaunt.builder import (
    build_module_context_artifacts,
    detect_api_changed_modules,
    detect_stale_modules,
    write_generated_module,
)
from jaunt.deps import build_spec_graph
from jaunt.digest import module_digest
from jaunt.module_api import module_api_digest
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


def test_detect_stale_modules_generation_fingerprint_change(tmp_path: Path) -> None:
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
    digest = module_digest("m", [e], specs, spec_graph)

    write_generated_module(
        package_dir=src,
        generated_dir="__generated__",
        module_name="m",
        source="def A():\n    return 1\n",
        header_fields={
            "tool_version": "0",
            "kind": "build",
            "source_module": "m",
            "module_digest": digest,
            "generation_fingerprint": "legacy-build",
            "spec_refs": [str(e.spec_ref)],
        },
    )

    stale = detect_stale_modules(
        package_dir=src,
        generated_dir="__generated__",
        module_specs=module_specs,
        specs=specs,
        spec_graph=spec_graph,
        generation_fingerprint="aider-build",
    )
    assert stale == {"m"}


def test_detect_api_changed_modules_only_flags_api_differences(tmp_path: Path) -> None:
    src = tmp_path / "src"
    spec_path = tmp_path / "m.py"
    _write(
        spec_path,
        """
def A(x: int) -> int:
    return x + 1
""".lstrip(),
    )
    entry = _entry(module="m", qualname="A", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    digest = module_digest("m", [entry], specs, spec_graph)
    api_digest = module_api_digest([entry])

    write_generated_module(
        package_dir=src,
        generated_dir="__generated__",
        module_name="m",
        source="def A(x: int) -> int:\n    return x + 1\n",
        header_fields={
            "tool_version": "0",
            "kind": "build",
            "source_module": "m",
            "module_digest": digest,
            "module_api_digest": api_digest,
            "spec_refs": [str(entry.spec_ref)],
        },
    )

    _write(
        spec_path,
        """
def A(x: int) -> int:
    y = x + 1
    return y
""".lstrip(),
    )
    entry_after = _entry(module="m", qualname="A", source_file=str(spec_path))

    changed = detect_api_changed_modules(
        package_dir=src,
        generated_dir="__generated__",
        module_specs={"m": [entry_after]},
        module_api_digests={"m": module_api_digest([entry_after])},
    )
    assert changed == set()

    _write(
        spec_path,
        """
def A(x: str) -> int:
    return len(x)
""".lstrip(),
    )
    entry_sig_change = _entry(module="m", qualname="A", source_file=str(spec_path))
    changed = detect_api_changed_modules(
        package_dir=src,
        generated_dir="__generated__",
        module_specs={"m": [entry_sig_change]},
        module_api_digests={"m": module_api_digest([entry_sig_change])},
    )
    assert changed == {"m"}


def test_build_module_context_blueprint_preserves_source_order(tmp_path: Path) -> None:
    spec_path = tmp_path / "pkg" / "auth_specs.py"
    _write(
        spec_path,
        (
            '"""Authentication specs."""\n\n'
            "from dataclasses import dataclass\n\n"
            "@dataclass(frozen=True)\n"
            "class Claims:\n"
            "    subject: str\n\n"
            "@magic\n"
            "def create_token(subject: str) -> str:\n"
            '    """Create a signed token."""\n'
            "    raise NotImplementedError\n\n"
            "DEFAULT_TTL = 3600\n\n"
            "@magic\n"
            "class AuthService:\n"
            '    """Authenticate and issue tokens."""\n'
            "    def issue(self, subject: str) -> str:\n"
            "        raise NotImplementedError\n"
        ),
    )
    create_token = _entry(
        module="pkg.auth_specs",
        qualname="create_token",
        source_file=str(spec_path),
    )
    auth_service = _entry(
        module="pkg.auth_specs",
        qualname="AuthService",
        source_file=str(spec_path),
    )
    artifacts = build_module_context_artifacts(
        module_name="pkg.auth_specs",
        entries=[create_token, auth_service],
        expected_names=["create_token", "AuthService"],
        module_specs={"pkg.auth_specs": [create_token, auth_service]},
        module_dag={"pkg.auth_specs": set()},
        package_dir=tmp_path,
        generated_dir="__generated__",
    )

    blueprint = artifacts.blueprint_source

    claims_idx = blueprint.index("# handwritten class already defined in `pkg.auth_specs`: Claims")
    create_idx = blueprint.index("def create_token(subject: str) -> str:")
    ttl_idx = blueprint.index(
        "# handwritten assignment already defined in `pkg.auth_specs`: DEFAULT_TTL"
    )
    service_idx = blueprint.index("class AuthService:")

    assert claims_idx < create_idx < ttl_idx < service_idx
    assert "# Reference-only blueprint for `pkg.auth_specs`." in blueprint
    assert "# from pkg.auth_specs import (" in blueprint
    assert "#     Claims," in blueprint
    assert "#     DEFAULT_TTL," in blueprint
    assert "class Claims:" not in blueprint
    assert "DEFAULT_TTL = 3600" not in blueprint
    assert "raise NotImplementedError" not in blueprint
    assert "def issue(self, subject: str) -> str:\n        ..." in blueprint
