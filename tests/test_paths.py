from __future__ import annotations

from pathlib import Path

from jaunt.paths import (
    generated_module_to_relpath,
    module_to_relpath,
    spec_module_to_generated_module,
)


def test_top_level_module_paths() -> None:
    # A top-level (single-segment) spec module maps into the generated dir as a
    # flat file `__generated__/<module>.py` — NOT a `<module>/__generated__/`
    # directory sibling (the 1.3.0 layout fix, findings 6/9/12).
    assert module_to_relpath("my_project") == Path("my_project") / "__init__.py"
    gen_mod = spec_module_to_generated_module("my_project")
    assert gen_mod == "__generated__.my_project"
    assert generated_module_to_relpath(gen_mod) == (Path("__generated__") / "my_project.py")


def test_top_level_module_paths_idempotent() -> None:
    # Mapping an already-generated top-level module is a no-op.
    gen_mod = spec_module_to_generated_module("timing")
    assert gen_mod == "__generated__.timing"
    assert spec_module_to_generated_module(gen_mod) == "__generated__.timing"
    assert generated_module_to_relpath(gen_mod) == (Path("__generated__") / "timing.py")


def test_nested_module_paths() -> None:
    # Package members are unchanged: pkg.mod -> pkg/__generated__/mod.py.
    assert module_to_relpath("my_project.sub.mod") == Path("my_project") / "sub" / "mod.py"
    gen_mod = spec_module_to_generated_module("my_project.sub.mod")
    assert gen_mod == "my_project.__generated__.sub.mod"
    assert generated_module_to_relpath(gen_mod) == (
        Path("my_project") / "__generated__" / "sub" / "mod.py"
    )


def test_package_member_paths() -> None:
    gen_mod = spec_module_to_generated_module("pkg.mod")
    assert gen_mod == "pkg.__generated__.mod"
    assert generated_module_to_relpath(gen_mod) == (Path("pkg") / "__generated__" / "mod.py")


def test_custom_generated_dir_top_level_module_paths() -> None:
    gen_mod = spec_module_to_generated_module("my_project", generated_dir="gen")
    assert gen_mod == "gen.my_project"
    assert generated_module_to_relpath(gen_mod, generated_dir="gen") == (
        Path("gen") / "my_project.py"
    )


def test_custom_generated_dir_nested_module_paths() -> None:
    gen_mod = spec_module_to_generated_module("my_project.sub.mod", generated_dir="gen")
    assert gen_mod == "my_project.gen.sub.mod"
    assert generated_module_to_relpath(gen_mod, generated_dir="gen") == (
        Path("my_project") / "gen" / "sub" / "mod.py"
    )
