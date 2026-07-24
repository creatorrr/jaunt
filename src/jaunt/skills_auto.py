from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from jaunt.external_imports import discover_external_distributions_with_warnings, pep503_normalize
from jaunt.errors import JauntBudgetExceededError, JauntQuotaGenerationError
from jaunt.pypi import PyPIReadmeError, fetch_readme
from jaunt.skill_agent import strip_leading_frontmatter
from jaunt.skill_manager import _atomic_write_text

_logger = logging.getLogger("jaunt.skills_auto")

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from jaunt.codex_executor import ModelCallRunner
    from jaunt.config import AgentConfig, CodexConfig, LLMConfig, SkillsConfig


def _is_local_install(dist: str) -> bool:
    """True when `dist` was installed from a local path/editable/VCS, not a PyPI index.

    Uses PEP 610 `direct_url.json`: presence of `dir_info` (local dir / editable) or
    `vcs_info`, or a `file://` URL, all mean "not a PyPI index install". Index installs
    have no `direct_url.json` (importlib returns None / raises), so they resolve to False.
    """
    try:
        d = metadata.distribution(dist)
    except Exception:
        return False
    try:
        raw = d.read_text("direct_url.json")
    except Exception:
        raw = None
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except Exception:
        return False
    if not isinstance(info, dict):
        return False
    if "dir_info" in info or "vcs_info" in info:
        return True
    url = info.get("url")
    return isinstance(url, str) and url.startswith("file://")


_HEADING_MARKER = "Missing required heading"


def _dist_from_generation_warning(warning: str) -> str | None:
    prefix = "Failed generating skill for "
    if not warning.startswith(prefix):
        return None
    rest = warning[len(prefix) :]
    token = rest.split("==", 1)[0].split(":", 1)[0].strip()
    return token or None


def _dedupe_heading_warnings(warnings: list[str]) -> list[str]:
    """Collapse per-dist 'Missing required heading' warnings into one summary line."""
    kept: list[str] = []
    affected: list[str] = []
    for w in warnings:
        if _HEADING_MARKER in w:
            affected.append(_dist_from_generation_warning(w) or w)
        else:
            kept.append(w)
    if affected:
        uniq = sorted(dict.fromkeys(affected))
        kept.append(
            f"{len(uniq)} generated skill(s) were missing required section headings: "
            + ", ".join(uniq)
        )
    return kept


@dataclass(frozen=True, slots=True)
class PyPISkillsResult:
    warnings: list[str]
    generation_failures: int = 0
    dists: dict[str, str] = field(default_factory=dict)


def skill_md_path(*, project_root: Path, dist: str) -> Path:
    dist_norm = pep503_normalize(dist)
    return (project_root / ".agents" / "skills" / dist_norm / "SKILL.md").resolve()


def _read_frontmatter(text: str) -> dict[str, str] | None:
    raw = text or ""
    if not raw.startswith("---\n"):
        return None
    end = raw.find("\n---", 4)
    if end == -1:
        return None
    block = raw[4:end]
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        meta[key.strip()] = val.strip().strip('"')
    return meta


def parse_generated_skill_meta(text: str) -> tuple[str, str] | None:
    """Return (dist, version) for a Jaunt-generated skill, else None."""
    meta = _read_frontmatter(text)
    if not meta:
        return None
    dist = meta.get("x-jaunt-dist")
    version = meta.get("x-jaunt-version")
    if dist and version:
        return dist, version
    return None


def _format_generated_skill_file(*, dist: str, version: str, body_md: str) -> str:
    description = f"Use when generating Python code that imports or uses the {dist} library."
    body = strip_leading_frontmatter((body_md or "").strip()).strip()
    fm = (
        "---\n"
        f'name: "{dist}"\n'
        f'description: "{description}"\n'
        f"x-jaunt-dist: {dist}\n"
        f"x-jaunt-version: {version}\n"
        "---\n"
    )
    return fm + body + "\n"


async def ensure_pypi_skills(
    *,
    project_root: Path,
    source_roots: Sequence[Path],
    generated_dir: str,
    llm: LLMConfig,
    agent: AgentConfig | None = None,
    codex: CodexConfig | None = None,
    skills: SkillsConfig | None = None,
    model_call_runner: ModelCallRunner | None = None,
) -> PyPISkillsResult:
    """Ensure SKILL.md files exist (frontmatter format) for imported PyPI libs."""
    if skills is not None and not skills.auto:
        return PyPISkillsResult(warnings=[], generation_failures=0, dists={})

    warnings: list[str] = []
    dists, scan_warnings = discover_external_distributions_with_warnings(
        source_roots, generated_dir=generated_dir
    )
    warnings.extend(scan_warnings)

    generation_failures = 0
    if dists:
        generation_failures = await _generate_pypi_skills(
            project_root=project_root,
            dists=dists,
            llm=llm,
            agent=agent,
            codex=codex,
            warnings=warnings,
            model_call_runner=model_call_runner,
        )
    return PyPISkillsResult(
        warnings=_dedupe_heading_warnings(warnings),
        generation_failures=generation_failures,
        dists=dict(dists),
    )


async def _generate_pypi_skills(
    *,
    project_root: Path,
    dists: dict[str, str],
    llm: LLMConfig,
    agent: AgentConfig | None,
    codex: CodexConfig | None,
    warnings: list[str],
    model_call_runner: ModelCallRunner | None,
) -> int:
    """Phase 1+2: identify stale PyPI dists and generate skills concurrently.

    Returns the number of dists that failed to generate.
    """

    import asyncio

    failures = 0

    # Phase 1: identify which dists need (re)generation.
    to_generate: list[tuple[str, str, Path]] = []  # (dist, version, path)
    for dist, version in sorted(dists.items(), key=lambda kv: pep503_normalize(kv[0])):
        if _is_local_install(dist):
            _logger.debug(
                "Skipping skill generation for %r: not a PyPI install (workspace/editable/local).",
                dist,
            )
            continue
        path = skill_md_path(project_root=project_root, dist=dist)

        needs_generate = False
        existing_header: tuple[str, str] | None = None
        if not path.exists():
            needs_generate = True
        else:
            try:
                txt = path.read_text(encoding="utf-8")
                existing_header = parse_generated_skill_meta(txt)
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
            from jaunt.config import AgentConfig, CodexConfig
            from jaunt.skillgen import CodexSkillGenerator

            resolved_agent = agent or AgentConfig()
            resolved_codex = codex or CodexConfig()
            if model_call_runner is None:
                generator = CodexSkillGenerator(llm, resolved_agent, resolved_codex)
            else:
                generator = CodexSkillGenerator(
                    llm,
                    resolved_agent,
                    resolved_codex,
                    model_call_runner=model_call_runner,
                )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Failed initializing skill generator: {type(e).__name__}: {e}")
            failures += len(to_generate)

        if generator is not None:

            async def _generate_one(
                dist: str,
                version: str,
                path: Path,
            ) -> tuple[str, str, Path, str] | None:
                """Stage one generated skill in memory; return None on ordinary failure."""
                try:
                    readme, readme_type = fetch_readme(dist, version)
                except PyPIReadmeError as e:
                    warnings.append(str(e))
                    return None
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed fetching PyPI README for {dist}=={version}: "
                        f"{type(e).__name__}: {e}"
                    )
                    return None

                try:
                    md = await generator.generate_skill_markdown(dist, version, readme, readme_type)
                except (JauntBudgetExceededError, JauntQuotaGenerationError):
                    raise
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed generating skill for {dist}=={version}: {type(e).__name__}: {e}"
                    )
                    return None

                try:
                    content = _format_generated_skill_file(dist=dist, version=version, body_md=md)
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed formatting skill for {dist}=={version}: {type(e).__name__}: {e}"
                    )
                    return None

                return dist, version, path, content

            tasks = [
                asyncio.create_task(_generate_one(dist, version, path))
                for dist, version, path in to_generate
            ]
            try:
                # Ordinary per-distribution failures return None and remain
                # best-effort. A command-wide quota/cost stop is different: surface it
                # immediately and prevent sibling Codex calls or file writes from
                # completing after the fatal signal.
                results = await asyncio.gather(*tasks)
            except (JauntBudgetExceededError, JauntQuotaGenerationError):
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            # Publish only after every concurrent generation has reached an
            # ordinary success/failure outcome. A fatal command-wide limit above
            # therefore leaves even already-completed siblings entirely in memory.
            for staged in results:
                if staged is None:
                    failures += 1
                    continue
                dist, version, path, content = staged
                try:
                    _atomic_write_text(path, content)
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"Failed writing skill for {dist}=={version} to {path}: "
                        f"{type(e).__name__}: {e}"
                    )
                    failures += 1

    return failures
