from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jaunt.external_imports import discover_external_distributions_with_warnings, pep503_normalize
from jaunt.pypi import PyPIReadmeError, fetch_readme

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from jaunt.config import LLMConfig


_HEADER_PREFIX = "<!-- jaunt:skill=pypi"


@dataclass(frozen=True, slots=True)
class SkillsAutoResult:
    skills_block: str
    warnings: list[str]


def skill_md_path(*, project_root: Path, dist: str) -> Path:
    dist_norm = pep503_normalize(dist)
    return (project_root / ".agents" / "skills" / dist_norm / "SKILL.md").resolve()


def _parse_generated_header(first_line: str) -> tuple[str, str] | None:
    line = (first_line or "").strip()
    if not (line.startswith("<!--") and line.endswith("-->")):
        return None
    inner = line[len("<!--") : -len("-->")].strip()
    if not inner.startswith("jaunt:skill=pypi"):
        return None

    dist: str | None = None
    version: str | None = None
    for part in inner.split()[1:]:
        if part.startswith("dist="):
            dist = part[len("dist=") :]
        elif part.startswith("version="):
            version = part[len("version=") :]

    if dist and version:
        return dist, version
    return None


def _format_generated_skill_file(*, dist: str, version: str, body_md: str) -> str:
    hdr = f"{_HEADER_PREFIX} dist={dist} version={version} -->"
    body = (body_md or "").strip()
    return hdr + "\n" + body + "\n"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".jaunt-tmp-",
        suffix=".md",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


async def ensure_pypi_skills_and_block(
    *,
    project_root: Path,
    source_roots: Sequence[Path],
    generated_dir: str,
    llm: LLMConfig,
) -> SkillsAutoResult:
    """Best-effort: ensure skills exist for imported external PyPI libs.

    Returns a single concatenated injection block to pass to the code generator.
    """

    import asyncio

    warnings: list[str] = []
    dists, scan_warnings = discover_external_distributions_with_warnings(
        source_roots, generated_dir=generated_dir
    )
    warnings.extend(scan_warnings)

    if not dists:
        return SkillsAutoResult(skills_block="", warnings=warnings)

    # Phase 1: identify which dists need (re)generation.
    to_generate: list[tuple[str, str, Path]] = []  # (dist, version, path)
    for dist, version in sorted(dists.items(), key=lambda kv: pep503_normalize(kv[0])):
        path = skill_md_path(project_root=project_root, dist=dist)

        needs_generate = False
        existing_header: tuple[str, str] | None = None
        if not path.exists():
            needs_generate = True
        else:
            try:
                txt = path.read_text(encoding="utf-8")
                first = txt.splitlines()[0] if txt else ""
                existing_header = _parse_generated_header(first)
            except Exception as e:  # noqa: BLE001
                warnings.append(
                    f"failed reading existing skill for {dist}: {type(e).__name__}: {e}"
                )
                continue

            if existing_header is None:
                # User-managed file; never overwrite.
                needs_generate = False
            else:
                _existing_dist, existing_ver = existing_header
                if str(existing_ver).strip() != str(version).strip():
                    needs_generate = True

        if needs_generate:
            to_generate.append((dist, version, path))

    # Phase 2: generate skills concurrently.
    if to_generate:
        generator = None
        try:
            from jaunt.skillgen import OpenAISkillGenerator

            generator = OpenAISkillGenerator(llm)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Failed initializing OpenAI skill generator: {type(e).__name__}: {e}")

        if generator is not None:

            async def _generate_one(dist: str, version: str, path: Path) -> None:
                try:
                    readme, readme_type = fetch_readme(dist, version)
                except PyPIReadmeError as e:
                    warnings.append(str(e))
                    return
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed fetching PyPI README for {dist}=={version}: "
                        f"{type(e).__name__}: {e}"
                    )
                    return

                try:
                    md = await generator.generate_skill_markdown(dist, version, readme, readme_type)
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed generating skill for {dist}=={version}: {type(e).__name__}: {e}"
                    )
                    return

                try:
                    content = _format_generated_skill_file(dist=dist, version=version, body_md=md)
                    _atomic_write_text(path, content)
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed writing skill for {dist}=={version} to {path}: "
                        f"{type(e).__name__}: {e}"
                    )

            tasks = [_generate_one(dist, version, path) for dist, version, path in to_generate]
            await asyncio.gather(*tasks, return_exceptions=True)

    # Build injection block from whatever is on disk.
    sections: list[str] = []
    for dist, version in sorted(dists.items(), key=lambda kv: pep503_normalize(kv[0])):
        path = skill_md_path(project_root=project_root, dist=dist)
        if not path.exists():
            continue
        try:
            txt = path.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"failed reading skill for {dist}: {type(e).__name__}: {e}")
            continue

        lines = txt.splitlines()
        if lines and _parse_generated_header(lines[0]) is not None:
            body = "\n".join(lines[1:]).lstrip("\n")
        else:
            body = txt

        body = (body or "").strip()
        if not body:
            continue
        sections.append(f"## {dist}=={version}\n{body}\n")

    skills_block = "\n".join(sections).strip()
    return SkillsAutoResult(skills_block=skills_block, warnings=warnings)
