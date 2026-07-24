"""Digest-cached, model-written project-overview generator."""

from __future__ import annotations

import hashlib
from pathlib import Path

from jaunt.errors import JauntBudgetExceededError, JauntQuotaGenerationError
from jaunt.generate.shared import load_prompt, render_template
from jaunt.registry import SpecEntry


_DOC_SOURCES = (
    ("README.md", "README"),
    ("AGENTS.md", "AGENTS.md"),
    ("CLAUDE.md", "CLAUDE.md"),
)


def _doc_intro(path: Path, max_chars: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    out_lines: list[str] = []
    for line in text.splitlines():
        if out_lines and line.startswith("## "):
            break
        out_lines.append(line)
    intro = "\n".join(out_lines).strip()
    if len(intro) > max_chars:
        intro = intro[:max_chars].rstrip() + "\n...[truncated]"
    return intro


def build_project_docs_block(root: Path, *, max_chars: int) -> str:
    """Inject README and AGENTS/CLAUDE intros (each capped). AGENTS preferred over CLAUDE."""
    sections: list[str] = []
    seen_agent_doc = False
    for filename, heading in _DOC_SOURCES:
        if heading in ("AGENTS.md", "CLAUDE.md"):
            if seen_agent_doc:
                continue  # AGENTS.md wins if both exist (often a symlink anyway)
        path = root / filename
        if not path.is_file():
            continue
        intro = _doc_intro(path, max_chars)
        if not intro:
            continue
        if heading in ("AGENTS.md", "CLAUDE.md"):
            seen_agent_doc = True
        sections.append(f"### {heading}\n{intro}")
    return "\n\n".join(sections)


def project_spec_digest(module_specs: dict[str, list[SpecEntry]], repo_map_block: str) -> str:
    """SHA-256 over sorted spec sources and the repo map block."""
    h = hashlib.sha256()
    for module_name in sorted(module_specs):
        for entry in module_specs[module_name]:
            h.update(str(entry.spec_ref).encode())
            h.update(b"\x00")
            h.update(Path(entry.source_file).read_text(encoding="utf-8").encode())
            h.update(b"\x00")
    h.update(repo_map_block.encode())
    return h.hexdigest()


async def project_overview_block_for_build(
    *,
    root,
    cfg,
    module_specs,
    repo_map_block: str,
    backend,
    cost_tracker=None,
) -> str:
    """Return the project overview prose block for injection into build prompts.

    Returns '' immediately if cfg.context.overview is False.
    Delegates to load_or_build_overview for caching and model calls. When a
    cost_tracker is supplied, a fresh (non-cached) overview model call is charged
    against it so the build budget and cost summary account for it.
    """
    if not cfg.context.overview:
        return ""
    try:
        digest = project_spec_digest(module_specs, repo_map_block)
        docs = build_project_docs_block(root, max_chars=cfg.context.max_chars)
        return await load_or_build_overview(
            backend,
            repo_map_block=repo_map_block,
            project_docs=docs,
            digest=digest,
            state_dir=root / ".jaunt",
            enabled=True,
            prompts=cfg.prompts,
            cost_tracker=cost_tracker,
        )
    except (JauntBudgetExceededError, JauntQuotaGenerationError):
        # Command-wide cost and quota limits are hard stops, including for this
        # otherwise best-effort auxiliary model call.
        raise
    except Exception:  # noqa: BLE001 - other overview failures remain best-effort
        return ""


async def load_or_build_overview(
    backend,
    *,
    repo_map_block: str,
    project_docs: str,
    digest: str,
    state_dir: Path,
    enabled: bool,
    prompts,
    cost_tracker=None,
) -> str:
    """Return cached or freshly-generated project overview prose.

    Returns '' immediately when enabled is False.
    Uses state_dir/PROJECT_OVERVIEW.md as cache; invalidates when
    state_dir/project_overview.digest does not match digest. When a fresh overview
    is generated and cost_tracker is supplied, the model call's token usage is
    recorded against it.
    """
    if not enabled:
        return ""
    overview_path = state_dir / "PROJECT_OVERVIEW.md"
    digest_path = state_dir / "project_overview.digest"

    system = load_prompt(
        "project_overview_system.md",
        getattr(prompts, "project_overview_system", None) or None,
    )
    user_tmpl = load_prompt(
        "project_overview_user.md",
        getattr(prompts, "project_overview_user", None) or None,
    )
    user = render_template(user_tmpl, {"project_docs": project_docs, "repo_map": repo_map_block})

    # The cache key covers the exact model inputs (system + rendered user, which embeds
    # project_docs and the repo map) plus the incoming spec/repo digest. Editing README/AGENTS,
    # the repo map, the spec sources, or the overview prompt templates all invalidate the cache.
    cache_digest = hashlib.sha256(
        b"\x00".join((digest.encode(), system.encode(), user.encode()))
    ).hexdigest()
    if (
        digest_path.is_file()
        and digest_path.read_text(encoding="utf-8").strip() == cache_digest
        and overview_path.is_file()
    ):
        return overview_path.read_text(encoding="utf-8")

    complete_with_quota_retry = getattr(
        backend,
        "complete_text_with_usage_and_quota_retry",
        None,
    )
    complete_with_usage = getattr(backend, "complete_text_with_usage", None)
    if complete_with_quota_retry is not None:
        raw, usage = await complete_with_quota_retry(system=system, user=user)
    elif complete_with_usage is not None:
        raw, usage = await complete_with_usage(system=system, user=user)
    else:
        raw, usage = await backend.complete_text(system=system, user=user), None
    prose = raw.strip()
    if cost_tracker is not None and usage is not None:
        cost_tracker.record("__project_overview__", usage)
        cost_tracker.check_budget()

    state_dir.mkdir(parents=True, exist_ok=True)
    overview_path.write_text(prose, encoding="utf-8")
    digest_path.write_text(cache_digest, encoding="utf-8")
    return prose
