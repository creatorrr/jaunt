"""High-level repo-context entry points used by the CLI and build path."""

from __future__ import annotations

from pathlib import Path

from jaunt.repo_context import block as block_mod
from jaunt.repo_context import tree as tree_mod
from jaunt.repo_context.digests import TreeCache


def _source_roots(root: Path, cfg) -> list[Path]:
    from jaunt.workspace import expand_roots

    configured: list[tuple[list[str], str]] = []
    if cfg.version == 1:
        configured.append((cfg.paths.source_roots, "paths.source_roots"))
    else:
        if cfg.python_target is not None:
            configured.append((cfg.python_target.source_roots, "target.py.source_roots"))
        if cfg.typescript_target is not None:
            configured.append((cfg.typescript_target.source_roots, "target.ts.source_roots"))

    expanded: list[Path] = []
    for roots, setting in configured:
        expanded.extend(
            expand_roots(
                root,
                roots,
                setting=setting,
                require_one=True,
            )
        )
    return list(dict.fromkeys(path.resolve() for path in expanded))


def _generated_dirs(cfg) -> tuple[str, ...]:
    configured: list[str] = []
    if cfg.version == 1:
        configured.append(cfg.paths.generated_dir)
    else:
        if cfg.python_target is not None:
            configured.append(cfg.python_target.generated_dir)
        if cfg.typescript_target is not None:
            configured.append(cfg.typescript_target.generated_dir)
    return tuple(dict.fromkeys(configured))


class _JsonBackendAdapter:
    """Thin wrapper exposing ``complete_json`` over the Codex text backend.

    Used only when build-time enrichment is enabled. Parses the model's reply as
    JSON; returns ``{}`` on any parse failure (which triggers the AST fallback).
    """

    def __init__(self, backend) -> None:
        self._backend = backend

    async def complete_json(self, prompt: str) -> dict:
        import json

        text = await self._backend.complete_text(
            system="You produce strict JSON for a repository map.",
            user=prompt,
        )
        text = text.strip()
        if text.startswith("```"):
            # Strip a fenced ```json ... ``` block if the model wraps it.
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


def _build_enrich_backend(cfg):
    from jaunt.generate.codex_backend import CodexBackend

    return _JsonBackendAdapter(CodexBackend(cfg.codex, cfg.llm, cfg.prompts))


def sync_tree(*, root: Path, cfg, today: str, enrich: bool | None = None):
    cache = TreeCache(root / ".jaunt" / "tree-cache.json")
    effective_enrich = cfg.context.enrich if enrich is None else enrich
    backend = _build_enrich_backend(cfg) if effective_enrich else None
    map_path = root / cfg.context.repo_map_file
    project_name = root.name
    if map_path.exists():
        try:
            committed = tree_mod.TreeDoc.load(map_path)
            if committed.project_name:
                project_name = committed.project_name
        except Exception:  # noqa: BLE001 - malformed maps are replaced by explicit tree runs
            pass
    doc, result = tree_mod.sync(
        repo_root=root,
        source_roots=_source_roots(root, cfg),
        generated_dir=_generated_dirs(cfg),
        cache=cache,
        project_name=project_name,
        project_version=str(cfg.version),
        today=today,
        enrich=effective_enrich,
        backend=backend,
    )
    cache.save()
    doc.write(map_path)
    return doc, result


def repo_map_block_for_build(*, root: Path, cfg, today: str) -> str:
    del today
    if not cfg.context.repo_map:
        return ""
    try:
        path = root / cfg.context.repo_map_file
        if not path.exists():
            return ""
        doc = tree_mod.TreeDoc.load(path)
        return block_mod.render_repo_map(doc, max_chars=cfg.context.max_chars)
    except Exception:  # noqa: BLE001 - never block the build on map maintenance
        return ""


def check_drift(*, root: Path, cfg):
    import shutil

    path = root / cfg.context.repo_map_file
    if not path.exists():
        return tree_mod.SyncResult(added=["<treedocs.yaml missing>"])
    doc = tree_mod.TreeDoc.load(path)

    # Probe a throwaway copy of the real cache so added/removed/restaled counts
    # are accurate without mutating the canonical sidecar (--check is read-only).
    real_cache = root / ".jaunt" / "tree-cache.json"
    probe_path = root / ".jaunt" / "_drift_probe.json"
    probe_path.parent.mkdir(parents=True, exist_ok=True)
    if real_cache.exists():
        shutil.copyfile(real_cache, probe_path)
    elif probe_path.exists():
        probe_path.unlink()

    fresh, result = tree_mod.sync(
        repo_root=root,
        source_roots=_source_roots(root, cfg),
        generated_dir=_generated_dirs(cfg),
        cache=TreeCache(probe_path),
        project_name=root.name,
        project_version=str(cfg.version),
        today=doc.last_updated,
    )
    if result.added or result.removed or result.restaled or fresh.signature() != doc.signature():
        return (
            result
            if (result.added or result.removed or result.restaled)
            else tree_mod.SyncResult(restaled=["<signature mismatch>"])
        )
    return None
