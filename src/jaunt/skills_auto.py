from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jaunt.external_imports import discover_external_distributions_with_warnings, pep503_normalize
from jaunt.pypi import PyPIReadmeError, fetch_readme
from jaunt.skill_manager import _atomic_write_text

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from jaunt.config import LLMConfig


_HEADER_PREFIX = "<!-- jaunt:skill=pypi"


@dataclass(frozen=True, slots=True)
class SkillsAutoResult:
    skills_block: str
    warnings: list[str]
    generation_failures: int = 0


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

    warnings: list[str] = []
    dists, scan_warnings = discover_external_distributions_with_warnings(
        source_roots, generated_dir=generated_dir
    )
    warnings.extend(scan_warnings)

    generation_failures = 0
    if dists:
        # Phase 1+2: generate skills for PyPI dists that need it.
        generation_failures = await _generate_pypi_skills(
            project_root=project_root, dists=dists, llm=llm, warnings=warnings
        )

    # Phase 3: Build injection block from ALL skills on disk (auto + user).
    from jaunt.skill_manager import build_skills_block

    skills_block = build_skills_block(project_root, pypi_dists=dists)
    return SkillsAutoResult(
        skills_block=skills_block, warnings=warnings, generation_failures=generation_failures
    )


async def _generate_pypi_skills(
    *,
    project_root: Path,
    dists: dict[str, str],
    llm: LLMConfig,
    warnings: list[str],
) -> int:
    """Phase 1+2: identify stale PyPI dists and generate skills concurrently.

    Returns the number of dists that failed to generate.
    """

    import asyncio

    failures = 0

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
            failures += len(to_generate)

        if generator is not None:

            async def _generate_one(dist: str, version: str, path: Path) -> bool:
                """Returns True on success, False on failure."""
                try:
                    readme, readme_type = fetch_readme(dist, version)
                except PyPIReadmeError as e:
                    warnings.append(str(e))
                    return False
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed fetching PyPI README for {dist}=={version}: "
                        f"{type(e).__name__}: {e}"
                    )
                    return False

                try:
                    md = await generator.generate_skill_markdown(dist, version, readme, readme_type)
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed generating skill for {dist}=={version}: {type(e).__name__}: {e}"
                    )
                    return False

                try:
                    content = _format_generated_skill_file(dist=dist, version=version, body_md=md)
                    _atomic_write_text(path, content)
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed writing skill for {dist}=={version} to {path}: "
                        f"{type(e).__name__}: {e}"
                    )
                    return False

                return True

            tasks = [_generate_one(dist, version, path) for dist, version, path in to_generate]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if r is not True:
                    failures += 1

    return failures
