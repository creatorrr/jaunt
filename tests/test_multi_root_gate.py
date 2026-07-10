"""Per-module replacement for the Jaunt 1.5 multi-root routing gate."""

from __future__ import annotations

from pathlib import Path

from jaunt import cli
from jaunt.status_core import enforce_source_root_routing


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_SPEC = (
    "import jaunt\n"
    "\n"
    "@jaunt.magic()\n"
    "def greet(name: str) -> str:\n"
    '    """Say hello."""\n'
    '    raise RuntimeError("stub")\n'
)


def _default_root_project(tmp_path: Path) -> Path:
    """Nested default roots (["srcroot", "."]) with specs under srcroot -> passes."""
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["srcroot", "."]\n\n[build]\nemit_stubs = false\n',
    )
    _write(tmp_path / "srcroot" / "mrgate_pkg" / "__init__.py", "")
    _write(tmp_path / "srcroot" / "mrgate_pkg" / "specs.py", _SPEC)
    return tmp_path


def _two_root_project(tmp_path: Path) -> Path:
    """Specs under BOTH configured roots -> spans multiple roots."""
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["pkg_a", "pkg_b"]\n\n'
        "[build]\nemit_stubs = false\n",
    )
    _write(tmp_path / "pkg_a" / "mrgate_a" / "__init__.py", "")
    _write(tmp_path / "pkg_a" / "mrgate_a" / "specs.py", _SPEC)
    _write(tmp_path / "pkg_b" / "mrgate_b" / "__init__.py", "")
    _write(tmp_path / "pkg_b" / "mrgate_b" / "specs.py", _SPEC)
    return tmp_path


def _wrong_order_project(tmp_path: Path) -> Path:
    """First existing root has no specs; specs live under a later root."""
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["empty_first", "pkg_b"]\n\n'
        "[build]\nemit_stubs = false\n",
    )
    # empty_first exists but carries no governed specs.
    (tmp_path / "empty_first").mkdir(parents=True, exist_ok=True)
    _write(tmp_path / "empty_first" / "notes.txt", "no specs here\n")
    _write(tmp_path / "pkg_b" / "mrgate_b" / "__init__.py", "")
    _write(tmp_path / "pkg_b" / "mrgate_b" / "specs.py", _SPEC)
    return tmp_path


# --- helper-level unit tests -------------------------------------------------


def test_helper_passes_nested_default_roots(tmp_path: Path) -> None:
    root = _default_root_project(tmp_path)
    from jaunt.registry import SpecEntry
    from jaunt.spec_ref import SpecRef

    entry = SpecEntry(
        kind="magic",
        spec_ref=SpecRef("mrgate_pkg.specs:greet"),
        module="mrgate_pkg.specs",
        qualname="greet",
        source_file=str(root / "srcroot" / "mrgate_pkg" / "specs.py"),
        obj=None,
        decorator_kwargs={},
    )
    # Must not raise: srcroot is the longest-path owning root and the first existing.
    enforce_source_root_routing(
        source_dirs=[root / "srcroot", root / "."],
        module_specs={"mrgate_pkg.specs": [entry]},
    )


def test_legacy_helper_is_noop_on_spanning_roots(tmp_path: Path) -> None:
    root = _two_root_project(tmp_path)
    from jaunt.registry import SpecEntry
    from jaunt.spec_ref import SpecRef

    def _entry(mod: str, rel: str) -> SpecEntry:
        return SpecEntry(
            kind="magic",
            spec_ref=SpecRef(f"{mod}:greet"),
            module=mod,
            qualname="greet",
            source_file=str(root / rel),
            obj=None,
            decorator_kwargs={},
        )

    enforce_source_root_routing(
        source_dirs=[root / "pkg_a", root / "pkg_b"],
        module_specs={
            "mrgate_a.specs": [_entry("mrgate_a.specs", "pkg_a/mrgate_a/specs.py")],
            "mrgate_b.specs": [_entry("mrgate_b.specs", "pkg_b/mrgate_b/specs.py")],
        },
    )


def test_legacy_helper_is_noop_on_wrong_order(tmp_path: Path) -> None:
    root = _wrong_order_project(tmp_path)
    from jaunt.registry import SpecEntry
    from jaunt.spec_ref import SpecRef

    entry = SpecEntry(
        kind="magic",
        spec_ref=SpecRef("mrgate_b.specs:greet"),
        module="mrgate_b.specs",
        qualname="greet",
        source_file=str(root / "pkg_b" / "mrgate_b" / "specs.py"),
        obj=None,
        decorator_kwargs={},
    )
    enforce_source_root_routing(
        source_dirs=[root / "empty_first", root / "pkg_b"],
        module_specs={"mrgate_b.specs": [entry]},
    )


def test_helper_noop_without_specs(tmp_path: Path) -> None:
    root = _two_root_project(tmp_path)
    # No governed specs -> no gate even with multiple roots.
    enforce_source_root_routing(
        source_dirs=[root / "pkg_a", root / "pkg_b"],
        module_specs={},
    )


# --- integration through the CLI discovery paths -----------------------------


def test_status_passes_default_roots(tmp_path: Path, capsys) -> None:
    root = _default_root_project(tmp_path)
    # status does not call the backend; nested default roots must pass the gate.
    args = cli.parse_args(["status", "--root", str(root)])
    rc = cli.cmd_status(args)
    err = capsys.readouterr().err
    assert rc != cli.EXIT_CONFIG_OR_DISCOVERY
    assert "span multiple source_roots" not in err


def test_workspace_resolves_each_output_base(tmp_path: Path) -> None:
    root = _two_root_project(tmp_path)
    from jaunt.config import load_config
    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, load_config(root=root))
    assert workspace.route_for("mrgate_a.specs").output_base == (root / "pkg_a").resolve()
    assert workspace.route_for("mrgate_b.specs").output_base == (root / "pkg_b").resolve()


def test_check_reports_unbuilt_across_spanning_roots(tmp_path: Path, capsys) -> None:
    root = _two_root_project(tmp_path)
    args = cli.parse_args(["check", "--root", str(root)])
    rc = cli.cmd_check(args)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_PYTEST_FAILURE
    assert "mrgate_a.specs" in out
    assert "mrgate_b.specs" in out


def test_status_supports_spanning_roots(tmp_path: Path, capsys) -> None:
    root = _two_root_project(tmp_path)
    args = cli.parse_args(["status", "--root", str(root)])
    rc = cli.cmd_status(args)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "mrgate_a.specs" in out
    assert "mrgate_b.specs" in out


def test_status_supports_empty_first_root(tmp_path: Path, capsys) -> None:
    root = _wrong_order_project(tmp_path)
    args = cli.parse_args(["status", "--root", str(root)])
    rc = cli.cmd_status(args)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "mrgate_b.specs" in out


def test_test_no_build_accepts_spanning_roots(tmp_path: Path, capsys) -> None:
    root = _two_root_project(tmp_path)
    args = cli.parse_args(["test", "--root", str(root), "--no-build", "--no-run", "--no-progress"])
    rc = cli.cmd_test(args)
    capsys.readouterr()
    assert rc == cli.EXIT_OK
