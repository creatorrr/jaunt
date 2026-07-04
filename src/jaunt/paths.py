"""Pure helpers for mapping spec modules to generated modules and file paths."""

from __future__ import annotations

from pathlib import Path


def spec_module_to_generated_module(module: str, generated_dir: str = "__generated__") -> str:
    parts = module.split(".")
    if parts and parts[0] == generated_dir:
        return module
    if len(parts) == 1:
        return f"{generated_dir}.{parts[0]}"
    if len(parts) >= 2 and parts[1] == generated_dir:
        return module
    return ".".join([parts[0], generated_dir, *parts[1:]])


def module_to_relpath(module: str) -> Path:
    parts = module.split(".")
    if len(parts) == 1:
        return Path(parts[0]) / "__init__.py"
    parts = [*parts[:-1], f"{parts[-1]}.py"]
    return Path(*parts)


def generated_module_to_relpath(module: str, *, generated_dir: str = "__generated__") -> Path:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == generated_dir:
        return Path(generated_dir) / Path(*parts[1:-1]) / f"{parts[-1]}.py"
    if parts and parts[-1] == generated_dir:
        return Path(*parts) / "__init__.py"
    return module_to_relpath(module)
