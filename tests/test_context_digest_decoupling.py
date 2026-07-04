"""Finding 14: repo-map / package-context content must not restale siblings.

The per-module staleness digest (`build_module_context_artifacts(...).digest`,
stored in the generated header as `module_context_digest`) must be invariant to
package-context grounding that changes when *other* modules are added: the
package tree listing and sibling-module summaries. Adding a brand-new spec
module must not restale an unrelated, spec-unchanged sibling.
"""

from __future__ import annotations

from pathlib import Path

from jaunt.builder import build_module_context_artifacts
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _entry(module: str, qualname: str, source_file: Path) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=str(source_file),
        obj=object(),
        decorator_kwargs={},
        class_name=None,
    )


def _digest_for_a(
    *, package_dir: Path, module_specs: dict[str, list[SpecEntry]], module_dag: dict[str, set[str]]
) -> str:
    return build_module_context_artifacts(
        module_name="pkg.a",
        entries=module_specs["pkg.a"],
        expected_names=["alpha"],
        module_specs=module_specs,
        module_dag=module_dag,
        package_dir=package_dir,
        generated_dir="__generated__",
    ).digest


def test_adding_sibling_spec_does_not_change_context_digest(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    a = pkg / "a.py"
    a.write_text("def alpha() -> int:\n    ...\n", encoding="utf-8")
    ea = _entry("pkg.a", "alpha", a)

    digest_before = _digest_for_a(
        package_dir=tmp_path,
        module_specs={"pkg.a": [ea]},
        module_dag={"pkg.a": set()},
    )

    # Campaign scenario: a new sibling spec module lands in the same package.
    b = pkg / "b.py"
    b.write_text("def beta() -> int:\n    ...\n", encoding="utf-8")
    eb = _entry("pkg.b", "beta", b)

    digest_after = _digest_for_a(
        package_dir=tmp_path,
        module_specs={"pkg.a": [ea], "pkg.b": [eb]},
        module_dag={"pkg.a": set(), "pkg.b": set()},
    )

    assert digest_before == digest_after


def test_adding_plain_sibling_file_does_not_change_context_digest(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    a = pkg / "a.py"
    a.write_text("def alpha() -> int:\n    ...\n", encoding="utf-8")
    ea = _entry("pkg.a", "alpha", a)

    module_specs = {"pkg.a": [ea]}
    module_dag: dict[str, set[str]] = {"pkg.a": set()}
    digest_before = _digest_for_a(
        package_dir=tmp_path, module_specs=module_specs, module_dag=module_dag
    )

    # A new non-spec .py file changes the "Package tree" grounding only.
    (pkg / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
    digest_after = _digest_for_a(
        package_dir=tmp_path, module_specs=module_specs, module_dag=module_dag
    )

    assert digest_before == digest_after
