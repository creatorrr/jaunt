"""Inspect built wheel/sdist bytes for the TypeScript target's packaged resources."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

PROMPTS = (
    "build_module.md",
    "build_system.md",
    "design_system.md",
    "design_user.md",
    "test_module.md",
    "test_system.md",
)
SCHEMAS = (
    "contract-ir-v1.schema.json",
    "protocol-v1.schema.json",
    "fixtures/error.response.json",
    "fixtures/initialize.request.json",
    "fixtures/initialize.response.json",
)


def _require_suffixes(names: set[str], suffixes: tuple[str, ...], *, artifact: Path) -> None:
    missing = [suffix for suffix in suffixes if not any(name.endswith(suffix) for name in names)]
    if missing:
        raise SystemExit(f"{artifact.name} is missing packaged resources: {', '.join(missing)}")


def _inspect(path: Path) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        prompt_prefix = "jaunt/typescript/prompts/"
        schema_prefix = "jaunt/typescript/schemas/"
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path) as archive:
            names = set(archive.getnames())
        prompt_prefix = "src/jaunt/typescript/prompts/"
        schema_prefix = "schemas/jaunt-ts/"
    else:
        return

    _require_suffixes(
        names,
        tuple(f"{prompt_prefix}{name}" for name in PROMPTS),
        artifact=path,
    )
    _require_suffixes(
        names,
        tuple(f"{schema_prefix}{name}" for name in SCHEMAS),
        artifact=path,
    )
    forbidden = [
        name
        for name in names
        if "node_modules/typescript" in name
        or name.endswith("/typescript.js")
        or name.endswith("/tsserver.js")
        or name.endswith(".node")
    ]
    if forbidden:
        raise SystemExit(
            f"{path.name} bundles a compiler/native payload: {', '.join(sorted(forbidden))}"
        )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: inspect_typescript_distribution.py DIST_DIRECTORY")
    directory = Path(sys.argv[1])
    artifacts = sorted(path for path in directory.iterdir() if path.is_file())
    if not any(path.suffix == ".whl" for path in artifacts) or not any(
        path.name.endswith(".tar.gz") for path in artifacts
    ):
        raise SystemExit("expected one wheel and one source distribution")
    for artifact in artifacts:
        _inspect(artifact)
    print(f"verified TypeScript resources in {len(artifacts)} Python distributions")


if __name__ == "__main__":
    main()
