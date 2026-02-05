from __future__ import annotations

from pathlib import Path

from jaunt.paths import (
    generated_module_to_relpath,
    module_to_relpath,
    spec_module_to_generated_module,
)


def test_init_module_paths() -> None:
    assert module_to_relpath("my_project") == Path("my_project") / "__init__.py"
    gen_mod = spec_module_to_generated_module("my_project")
    assert gen_mod == "my_project.__generated__"
    assert generated_module_to_relpath(gen_mod) == (
        Path("my_project") / "__generated__" / "__init__.py"
    )


def test_nested_module_paths() -> None:
    assert module_to_relpath("my_project.sub.mod") == Path("my_project") / "sub" / "mod.py"
    gen_mod = spec_module_to_generated_module("my_project.sub.mod")
    assert gen_mod == "my_project.__generated__.sub.mod"
    assert generated_module_to_relpath(gen_mod) == (
        Path("my_project") / "__generated__" / "sub" / "mod.py"
    )


def test_custom_generated_dir_init_module_paths() -> None:
    gen_mod = spec_module_to_generated_module("my_project", generated_dir="gen")
    assert gen_mod == "my_project.gen"
    assert generated_module_to_relpath(gen_mod, generated_dir="gen") == (
        Path("my_project") / "gen" / "__init__.py"
    )


def test_custom_generated_dir_nested_module_paths() -> None:
    gen_mod = spec_module_to_generated_module("my_project.sub.mod", generated_dir="gen")
    assert gen_mod == "my_project.gen.sub.mod"
    assert generated_module_to_relpath(gen_mod, generated_dir="gen") == (
        Path("my_project") / "gen" / "sub" / "mod.py"
    )
